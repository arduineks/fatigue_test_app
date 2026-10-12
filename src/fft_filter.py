# -*- coding: utf-8 -*-
"""FFT-фильтр ОТОБРАЖАЕМЫХ рядов графика (RAW / FILTERED).

Назначение
----------
Данный модуль реализует FFT-фильтр ТОЛЬКО для отрисовки кривых RAW
и FILTERED. Он НЕ влияет на:
  * анализ циклов (process_cycle идёт по force_n);
  * регулятор поддержания силы;
  * детекцию разрыва;
  * запись сессий (SessionRecorder пишет сырые кадры).

То есть фильтр — исключительно косметика на экране: он строит
отдельный буфер отображаемых значений, выровненный по индексам с
исходным рядом, а исходные буферы (raw_values / filtered_values)
остаются нетронутыми.

ВАЖНО про ряд FILTERED: на МК он УЖЕ сглажен — сырой счёт →
медианный фильтр (n=3) → IIR (force_filter_default_alpha = 0.1).
IIR с α=0.1 — жёсткий НЧ: FFT-фильтр на FILTERED обычно ничего
не меняет (в этом ряде почти нет энергии выше десятков Гц).
Осмысленно применять FFT-фильтр к RAW; на FILTERED оставлен для
экспериментов. Учтите также фазовую задержку IIR при сопоставлении
RAW/FILTERED по времени.

Как работает
------------
* Схема — потоковый overlap-add (STFT), НЕ «пересчёт всего буфера»:
  пересчёт всего буфера на каждом тике приводил к тому, что свежий
  кусок (нарисованный сырым на прошлом кадре) ЗАМЕНЯЛСЯ
  отфильтрованным — на low-pass это выглядит как дёрганье графика
  каждые 0.25 с (на notch незаметно — вырез узкий). В потоковой
  схеме каждый сэмпл вычисляется ОДИН раз и больше не меняется.
* Fs оценивается по ВСЕМУ окну: fs = (n-1)/(t[-1]-t[0]). Медиана dt
  НЕ годится: кадры приходят с порта ПАЧКАМИ (воркер читает блок
  байт — N кадров с почти одинаковым timestamp, затем пауза) → dt
  бимодальный (0 и ~20 мс), медиана завышала Fs ~вдвое (2097 Гц при
  реальных 1000) и маски попадали НЕ в те бины — фильтр визуально
  «не работал» (фидбек стенда 12-10-2026).
* Окно фильтра W = 3·hop, hop = HOP_SECONDS (0.25 с). На окне:
  cosine edge-fade краёв (0.05 с — против растекания спектра
  прямоугольного окна), rfft, маска по режиму, irfft. В выходной
  буфер отдаётся ТОЛЬКО центральная треть окна [H, 2H) — она дальше
  всех от краёв окна, краевые эффекты туда не попадают. Хвост
  буфера (последние ~1-2 hop) остаётся сырым до обработки следующих
  окон — задержка отображения ~0.25-0.5 с.
* Маска с плавными переходами (cosine-транзишн):
  notch 50 — полное зануление 50±1.5 Гц, спад до 50±4 Гц;
  low-pass fc — пропуск до fc, спад fc → fc+5 Гц.
* Исторические отбраковки (замеры):
  - «Hann-окно + компенсация делением в time-domain» — деление
    возвращает энергию в занулённые бины (notch ~1x);
  - зеркальный padding на коротком (2 с) окне — отражённая
    синусоида даёт splatter за вырез (~7x);
  - пересчёт всего буфера — дёрганье low-pass (см. выше).

Стабильность
------------
Импорт numpy обёрнут в try/except: если numpy нет, модуль всё равно
импортируется, NUMPY_AVAILABLE=False, а вызывающий виджет показывает
пункт меню «FFT-фильтр» серым и работает как раньше.
"""

import time as _time

from src.logging_setup import fft_logger, _ensure_fft_log_handler

try:
    import numpy as _np
    NUMPY_AVAILABLE = True
except Exception:          # pragma: no cover — зависит от окружения
    _np = None
    NUMPY_AVAILABLE = False


# Идентификаторы и подписи режимов (порядок = порядок в меню).
# lp10/lp20 удалены по фидбеку стенда: на частоте циклов ~1-2 Гц
# срезают полезный сигнал.
FFT_FILTER_MODES = (
    ("off", "Выключен"),
    ("lp50", "Low-pass 50 Гц"),
    ("notch50", "Notch 50 Гц (сеть)"),
)


class FftDisplayFilter:
    """Потоковый FFT-фильтр (overlap-add) для одного ряда."""

    HOP_SECONDS = 0.25          # шаг сетки окон и emissия за раз
    WINDOW_HOPS = 5             # окно = 5 hop (1.25 с): длиннее окно —
                                # уже утечка маски; латентность = 2 hop
    EDGE_FADE_SECONDS = 0.05    # fade краёв ВНУТРИ окна
    # Пределы оценки Fs по меткам времени; вне них — откат на дефолт.
    FS_MIN = 1.0
    FS_MAX = 20000.0

    def __init__(self, default_fs=600.0):
        self.mode = "off"
        # Буфер отображаемых значений, выровненный по индексам с
        # исходным рядом (raw_values / filtered_values).
        self.buffer = []
        self._default_fs = float(default_fs)
        self._fs = None
        self._fs_updated_at = 0.0
        # Время последнего кадра на момент последнего пересчёта —
        # для троттлинга.
        self._processed_upto_time = None
        # Сколько сэмплов источника УЖЕ финализировано в буфер
        # (индекс в координатах источника; сдвигается trim_front).
        # None = фильтр с нуля (смена режима / сброс).
        self._processed_upto = 0
        self._seen_fs = None      # fs, при которой шла обработка

    # ------------------------------------------------------------------
    # управление режимом
    # ------------------------------------------------------------------

    def set_mode(self, mode, series="?"):
        """Установить режим фильтра (см. FFT_FILTER_MODES).

        Неизвестный идентификатор игнорируется (остаётся прежний
        режим) — защита от опечаток вызывающего кода.
        """
        if mode != "off" and mode not in dict(FFT_FILTER_MODES):
            return
        if mode != self.mode:
            # Смена режима → пересчёт с нуля (иначе старый
            # фильтрованный хвост смешивается с новым).
            self._processed_upto = None
        self.mode = mode
        self._processed_upto_time = None
        _ensure_fft_log_handler()
        fft_logger.debug(
            "MODE | series=%s mode=%s", series, mode
        )

    def is_on(self):
        return self.mode != "off" and NUMPY_AVAILABLE

    # ------------------------------------------------------------------
    # синхронизация буфера с исходным рядом
    # ------------------------------------------------------------------

    def append(self, value):
        """Новый кадр: пока не обработан, храним сырое значение."""
        self.buffer.append(value)

    def trim_front(self, count):
        """Удалить count старейших элементов (вслед за исходным рядом)."""
        if count <= 0:
            return
        if count >= len(self.buffer):
            del self.buffer[:]
        else:
            del self.buffer[:count]
        # Индексы источника сдвинулись влево.
        if self._processed_upto is not None:
            self._processed_upto = max(0, self._processed_upto - count)

    def reset(self):
        """Полный сброс (новая сессия / очистка графика)."""
        self.buffer.clear()
        self._processed_upto = 0
        self._processed_upto_time = None
        self._seen_fs = None

    def ensure_length(self, length):
        """Привести длину буфера к length (для пересчёта/тестов)."""
        if length < 0:
            length = 0
        if len(self.buffer) > length:
            del self.buffer[length:]
        while len(self.buffer) < length:
            self.buffer.append(self.buffer[-1] if self.buffer else 0.0)

    # ------------------------------------------------------------------
    # оценка Fs
    # ------------------------------------------------------------------

    def _estimate_fs(self, frame_times, now):
        # Кэш: не пересчитываем Fs чаще, чем раз в 2 с.
        if (
                self._fs is not None
                and (now - self._fs_updated_at) < 2.0
        ):
            return self._fs

        fs = None
        n = len(frame_times)
        # Оценка Fs по ВСЕМУ окну: fs = (n-1)/(t[-1]-t[0]) —
        # устойчива к пачкам (см. докстринг модуля).
        span = frame_times[-1] - frame_times[0]
        if n >= 2 and span > 0:
            cand = (n - 1) / span
            if self.FS_MIN <= cand <= self.FS_MAX:
                fs = cand

        if fs is None:
            fs = self._default_fs

        self._fs = fs
        self._fs_updated_at = now
        return fs

    # ------------------------------------------------------------------
    # маска по режиму
    # ------------------------------------------------------------------

    def _apply_mode(self, spectrum, freqs):
        m = self.mode
        if m == "off":
            return spectrum
        if m.startswith("lp"):
            try:
                fc = float(m[2:])
            except ValueError:
                return spectrum
            # Пропуск до fc, cosine-спад в (fc .. fc+5 Гц), ноль
            # выше. Переход фиксированной ширины 5 Гц (а не
            # 0.25*fc): при fc=50 плавный спад до 62.5 Гц давал
            # лишь ~10x подавления сети 60 Гц (фидбек стенда).
            fw = min(5.0, fc * 0.25)
            mask = _np.where(
                freqs <= fc,
                1.0,
                _np.where(
                    freqs <= fc + fw,
                    0.5 + 0.5 * _np.cos(
                        _np.pi * (freqs - fc) / fw
                    ),
                    0.0,
                ),
            )
            return spectrum * mask
        if m == "notch50":
            # Полное зануление 50±1.5 Гц, cosine-переход до 50±4 Гц.
            d = _np.abs(freqs - 50.0)
            mask = _np.where(
                d <= 1.5,
                0.0,
                _np.where(
                    d <= 4.0,
                    0.5 + 0.5 * _np.cos(_np.pi * (d - 1.5) / 2.5),
                    1.0,
                ),
            )
            return spectrum * mask
        return spectrum

    # ------------------------------------------------------------------
    # потоковый пересчёт (overlap-add)
    # ------------------------------------------------------------------

    def refresh(self, source, frame_times, series="?"):
        """Обработать накопившиеся окна; вернуть True, если было что
        финализировать.

        source      — исходный ряд (raw_values / filtered_values);
        frame_times — параллельные метки unix-времени кадров;
        series      — имя ряда для лога ("RAW" / "FILTERED").
        """
        if not NUMPY_AVAILABLE or self.mode == "off":
            return False
        if _np is None:
            return False
        _t0 = _time.perf_counter()

        n = len(source)
        if n < 32 or len(frame_times) != n:
            return False
        if len(self.buffer) != n:
            self.ensure_length(n)

        now = frame_times[-1]

        # Троттлинг по времени последнего кадра.
        if (
                self._processed_upto_time is not None
                and (now - self._processed_upto_time) < self.HOP_SECONDS
                and self._processed_upto is not None
        ):
            return False

        fs = self._estimate_fs(frame_times, now)
        if not fs or fs <= 0:
            return False

        hop = max(8, int(round(self.HOP_SECONDS * fs)))
        nwin = hop * self.WINDOW_HOPS          # окно = 3 hop
        fade = max(2, int(round(self.EDGE_FADE_SECONDS * fs)))
        fade = min(fade, hop // 2)

        if (
                self._processed_upto is None
                or self._seen_fs is None
                or abs(fs - self._seen_fs) > 0.05 * self._seen_fs
        ):
            # Смена режима / СУЩЕСТВЕННОЕ изменение Fs (>5%):
            # переобработка ВСЕЙ истории с нуля. Малый дрейф оценки
            # (±пара Гц от джиттера span) НЕ должен триггерить
            # пересчёт с нуля — иначе финализированная часть
            # переписывается и график дёргается.
            self._processed_upto = 0
            self._seen_fs = fs

        emitted = 0
        rms_in_sum = 0.0
        rms_out_sum = 0.0

        # Окна с шагом hop: каждое окно отдаёт центральный hop
        # [s+hop, s+2hop) — дальше всех от краёв окна.
        while self._processed_upto + nwin <= n:
            s = self._processed_upto

            x = _np.asarray(source[s:s + nwin], dtype=float)
            if x.size != nwin:
                break

            # Edge-fade краёв окна (cosine к среднему окна):
            # краевые эффекты маскирования в emit-зону [hop, 2hop)
            # не попадают (она ≥ hop от краёв).
            if fade >= 2:
                wmean = float(x.mean())
                ramp = 0.5 * (1.0 - _np.cos(
                    _np.pi * _np.arange(fade) / fade
                ))
                x = x.copy()
                x[:fade] = wmean + (x[:fade] - wmean) * ramp
                x[-fade:] = wmean + (x[-fade:] - wmean) * ramp[::-1]

            spectrum = _np.fft.rfft(x)
            freqs = _np.fft.rfftfreq(nwin, d=1.0 / fs)
            spectrum = self._apply_mode(spectrum, freqs)
            y = _np.fft.irfft(spectrum, n=nwin)

            if s == 0:
                # Самое первое окно: отдаём и левый край (это
                # старейшие данные — небольшая краевая погрешность
                # приемлема, иначе буфер не заполнится).
                seg_in = x[:hop]
                seg_out = y[:hop]
                self.buffer[:hop] = seg_out.tolist()
                emitted += hop
                rms_in_sum += float(_np.sum(seg_in ** 2))
                rms_out_sum += float(_np.sum(seg_out ** 2))

            seg_in = x[hop:2 * hop]
            seg_out = y[hop:2 * hop]
            self.buffer[s + hop:s + 2 * hop] = seg_out.tolist()
            emitted += hop
            rms_in_sum += float(_np.sum(seg_in ** 2))
            rms_out_sum += float(_np.sum(seg_out ** 2))

            self._processed_upto = s + hop

        if self._processed_upto_time is None or emitted:
            self._processed_upto_time = now

        if not emitted:
            return False

        # Метрики качества фильтра: RMS вход/выход на emitованной
        # части и время пересчёта — в logs/fft_filter.log (rotating).
        rms_in = (rms_in_sum / emitted) ** 0.5 if emitted else 0.0
        rms_out = (rms_out_sum / emitted) ** 0.5 if emitted else 0.0
        _ensure_fft_log_handler()
        fft_logger.debug(
            "REFRESH | series=%s mode=%s fs=%.1f n=%d emitted=%d "
            "rms_in=%.4f rms_out=%.4f att_db=%.1f ms=%.1f",
            series,
            self.mode,
            fs,
            n,
            emitted,
            rms_in,
            rms_out,
            (
                20.0 * _np.log10(rms_in / rms_out)
                if rms_out > 1e-12
                else 0.0
            ),
            (_time.perf_counter() - _t0) * 1000.0,
        )
        return True
