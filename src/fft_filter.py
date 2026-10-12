# -*- coding: utf-8 -*-
"""FFT-фильтр ОТОБРАЖАЕМЫХ рядов графика (RAW / FILTERED).

Назначение
----------
Данный модуль реализует ленивый FFT-фильтр ТОЛЬКО для отрисовки
кривых RAW и FILTERED. Он НЕ влияет на:
  * анализ циклов (process_cycle идёт по force_n);
  * регулятор поддержания силы;
  * детекцию разрыва;
  * запись сессий (SessionRecorder пишет сырые кадры).

То есть фильтр — исключительно косметика на экране: он строит
отдельный буфер отображаемых значений, выровненный по индексам с
исходным рядом, а исходные буферы (raw_values / filtered_values)
остаются нетронутыми.

Как работает
------------
* Fs оценивается по меткам времени кадров (медиана dt, обновляется
  не чаще раза в пару секунд). При аномалии dt — откат на значение
  по умолчанию (номинальная частота опроса).
* Пересчёт ленивый: не чаще одного раза в MIN_INTERVAL секунд
  (по времени последнего кадра). Фильтруется ВЕСЬ буфер сразу:
  rfft всего ряда, маска по режиму, irfft.
* Чтобы прямоугольное окно FFT не дало растекания спектра от
  скачка на границах, первые/последние EDGE_FADE секунд плавно
  сводятся к среднему (cosine-ramp). Граничные области все равно
  «косметика»: у графика самые старые точки уходят из окна.
* Маска с плавными переходами (cosine-транзишн ± half-ширина):
  notch 50 — полное зануление 50±1.5 Гц, спад до 50±4 Гц;
  low-pass fc — пропуск до fc, спад до 1.25·fc.
* Историческая попытка «Hann-окно + компенсация делением» была
  отбракована замером: деление на окно в time-domain возвращает
  энергию в занулённые бины (подавление 50 Гц ~1x).

Стабильность
------------
Импорт numpy обёрнут в try/except: если numpy нет, модуль всё равно
импортируется, NUMPY_AVAILABLE=False, а вызывающий виджет показывает
пункт меню «FFT-фильтр» серым и работает как раньше.
"""

import time

try:
    import numpy as _np
    NUMPY_AVAILABLE = True
except Exception:          # pragma: no cover — зависит от окружения
    _np = None
    NUMPY_AVAILABLE = False


# Идентификаторы и подписи режимов (порядок = порядок в меню).
FFT_FILTER_MODES = (
    ("off", "Выключен"),
    ("lp10", "Low-pass 10 Гц"),
    ("lp20", "Low-pass 20 Гц"),
    ("lp50", "Low-pass 50 Гц"),
    ("notch50", "Notch 50 Гц (сеть)"),
)


class FftDisplayFilter:
    """Ленивый FFT-фильтр для одного отображаемого ряда."""

    MIN_INTERVAL = 0.25
    EDGE_FADE_SECONDS = 0.25
    # Пределы оценки Fs по меткам времени; вне них — откат на дефолт.
    FS_MIN = 1.0
    FS_MAX = 20000.0

    def __init__(self, default_fs=330.0):
        self.mode = "off"
        # Буфер отображаемых значений, выровненный по индексам с
        # исходным рядом (raw_values / filtered_values).
        self.buffer = []
        self._default_fs = float(default_fs)
        self._fs = None
        self._fs_updated_at = 0.0
        # Время последнего кадра на момент последнего пересчёта —
        # для троттлинга MIN_INTERVAL.
        self._processed_upto_time = None

    # ------------------------------------------------------------------
    # управление режимом
    # ------------------------------------------------------------------

    def set_mode(self, mode):
        """Установить режим фильтра (см. FFT_FILTER_MODES).

        Неизвестный идентификатор игнорируется (остаётся прежний
        режим) — защита от опечаток вызывающего кода.
        """
        if mode != "off" and mode not in dict(FFT_FILTER_MODES):
            return
        self.mode = mode
        # Следующий refresh() пересчитает немедленно (сброс троттлинга).
        self._processed_upto_time = None

    def is_on(self):
        return self.mode != "off" and NUMPY_AVAILABLE

    # ------------------------------------------------------------------
    # синхронизация буфера с исходным рядом
    # ------------------------------------------------------------------

    def append(self, value):
        """Новый кадр: пока фильтр не пересчитан, храним сырое значение."""
        self.buffer.append(value)

    def trim_front(self, count):
        """Удалить count старейших элементов буфера (вслед за исходным рядом)."""
        if count <= 0:
            return
        if count >= len(self.buffer):
            del self.buffer[:]
        else:
            del self.buffer[:count]

    def reset(self):
        """Полный сброс (новая сессия / очистка графика)."""
        self.buffer.clear()
        self._processed_upto_time = None

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
        k = min(n - 1, 400)
        if k >= 2:
            dts = []
            for i in range(1, k + 1):
                dt = frame_times[-i] - frame_times[-i - 1]
                if dt > 0:
                    dts.append(dt)
            if dts:
                dts.sort()
                dt = dts[len(dts) // 2]
                cand = 1.0 / dt if dt > 0 else None
                if cand is not None and self.FS_MIN <= cand <= self.FS_MAX:
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
            # Пропуск до fc, cosine-спад в (fc .. 1.25*fc), ноль выше.
            mask = _np.where(
                freqs <= fc,
                1.0,
                _np.where(
                    freqs <= fc * 1.25,
                    0.5 + 0.5 * _np.cos(
                        _np.pi * (freqs - fc) / (0.25 * fc)
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
    # ленивый пересчёт
    # ------------------------------------------------------------------

    def refresh(self, source, frame_times):
        """Лениво пересчитать буфер отображаемых значений.

        source      — исходный ряд (raw_values / filtered_values);
        frame_times — параллельные метки unix-времени кадров.

        Возвращает True, если пересчёт выполнен.
        """
        if not NUMPY_AVAILABLE or self.mode == "off":
            return False
        if _np is None:
            return False

        n = len(source)
        if n < 32 or len(frame_times) != n:
            return False
        if len(self.buffer) != n:
            self.ensure_length(n)

        now = frame_times[-1]

        # Троттлинг: не чаще MIN_INTERVAL (по времени последнего кадра).
        if (
                self._processed_upto_time is not None
                and (now - self._processed_upto_time) < self.MIN_INTERVAL
        ):
            return False

        fs = self._estimate_fs(frame_times, now)
        if not fs or fs <= 0:
            return False

        x = _np.asarray(source, dtype=float)
        if x.size != n:
            return False

        # Edge-fade к среднему (cosine): убирает скачок на границах
        # прямоугольного окна FFT — иначе растекание спектра.
        nf = min(int(round(self.EDGE_FADE_SECONDS * fs)), n // 2)
        if nf >= 2:
            mean = float(x.mean())
            ramp = 0.5 * (1.0 - _np.cos(_np.pi * _np.arange(nf) / nf))
            x = x.copy()
            x[:nf] = mean + (x[:nf] - mean) * ramp
            x[-nf:] = mean + (x[-nf:] - mean) * ramp[::-1]

        spectrum = _np.fft.rfft(x)
        freqs = _np.fft.rfftfreq(n, d=1.0 / fs)
        spectrum = self._apply_mode(spectrum, freqs)
        y = _np.fft.irfft(spectrum, n=n)

        # Кроссфейд головы с предыдущим содержимым буфера —
        # сглаживает возможный стык пересчётов.
        ov = min(nf, n)
        if ov > 0:
            prev = _np.asarray(
                self.buffer[:ov],
                dtype=float,
            )
            ramp = _np.linspace(0.0, 1.0, ov)
            y[:ov] = ramp * y[:ov] + (1.0 - ramp) * prev

        self.buffer[:] = y.tolist()
        self._processed_upto_time = now
        return True
