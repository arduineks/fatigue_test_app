"""DataPipeline — обработка потока данных ВНЕ GUI-потока (шаг 1
изоляции потоков; подготовка к переходу на pyqtgraph).

Что перенесено из GUI-потока:
- разбор RX-буфера (parse_rx → измерительные кадры + текстовые строки),
- буферы данных (values/raw/filtered/times/positions) — копия того,
  что делал ForceGraphWidget.add_frame (обрезка истории чанком,
  LIVE-окно не нужно — окно считает GUI-график у себя),
- FSM анализа циклов (CycleAnalyzerMixin переиспользован как есть —
  он чистый, без Qt) + блок завершения цикла WAIT_MID_UP из
  add_frame,
- адаптивная оценка sample_rate (span по окну).

Разделение потоков:
- SerialWorker.data_received → queued-сигнал → pipeline.feed()
  выполняется В ПОТОКЕ pipeline;
- pipeline.emit batch_ready(frames, texts, cycle) → queued-сигнал
  → GUI-слот (add_frame_batch на графике + карточки).
  Батчи не чаще EMIT_INTERVAL_S (50 Гц), как у SerialWorker.

GUI-поток после этого не делает ни разбора байтов, ни FSM, ни
работы с буферами — только приём готовых снимков. Гейтинг циклов
(set_cycles_enabled / set_traverse_speed / _force_stop_active)
задаётся GUI синхронно (атомарные присваивания bool/float).

Флаг включения — src.protocol.USE_DATA_PIPELINE (по умолчанию
False: legacy-путь без изменений, включается на стенде одной
константой).
"""

import struct
import time
from collections import deque

from PyQt5.QtCore import QThread, pyqtSignal

from src.cycle_analyzer import CycleAnalyzerMixin
from src.logging_setup import logger


# Батч не чаще 20 мс (50 событий/с) — по опыту SerialWorker:
# продюсер не должен систематически опережать консюмера GUI.
EMIT_INTERVAL_S = 0.02
EMIT_THRESHOLD_BYTES = 4096

# Лимит RX-буфера (как в GUI-пути, RX_BUFFER_LIMIT).
RX_BUFFER_LIMIT = 65536

# История буферов: точек (120 с × SPS задаёт GUI через
# set_history_points); обрезка — чанком, как в add_frame.
TRIM_CHUNK = 512

FRAME_START = 0xAA
FRAME_END = 0xBB
FRAME_FORMAT = "<BiiffB"
FRAME_SIZE = struct.calcsize(FRAME_FORMAT)  # 18


class CycleEngine(CycleAnalyzerMixin):
    """Автономный движок FSM циклов.

    CycleAnalyzerMixin — чистая логика (math/time/logger, без Qt);
    недостающие атрибуты состояния заводит здесь. Завершение цикла
    (WAIT_MID_UP) — код из ForceGraphWidget.add_frame, перенесён
    без изменения семантики (счёт всегда, карточки — по гейту).
    """

    def __init__(self):
        self.macro_move_speed = 0.05
        self._cycles_enabled = False
        self._macro_moving = False
        self.traverse_speed_mm_s = None
        self._force_stop_active = False

        # Параметры FSM — дефолты ForceGraphWidget.__init__
        # (reset_cycle_analysis их не задаёт).
        self.cycle_tolerance = 0.03
        self.cycle_turn_confirm = 2

        self.reset_cycle_analysis()

    def process_frame(self, force_n):
        """Один кадр: FSM. Возвращает True, если цикл только что
        завершился (счётчик вырос).

        Завершение цикла (MID->MAX->MID->MIN->MID) уже внутри
        CycleAnalyzerMixin.process_cycle — здесь НЕ дублируется.
        """

        if self._force_stop_active:
            # Как в GUI-пути: при активном стопоре по силе
            # подсчёт циклов подавлен (мусорные «циклы»).
            self.cycle_prev_force = force_n
            return False

        before = self.cycle_count

        self.process_cycle(force_n)

        return self.cycle_count != before


class DataPipeline(QThread):
    """Поток-обработчик: байты порта → кадры, FSM, буферы."""

    # Готовый батч: (frames, texts, cycle_snapshot)
    # frames — список (raw, filtered, force_n, mm);
    # texts  — список строк (bytes, уже «как есть» куски);
    # cycle_snapshot — dict со срезом состояния FSM.
    batch_ready = pyqtSignal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._rx = bytearray()
        self._stop = False

        # Буферы данных (свой экземпляр — граф хранит свои).
        self.values = []
        self.raw_values = []
        self.filtered_values = []
        self.frame_times = []
        self.frame_positions = []
        self._max_points = 120000
        self._srate = 1000.0
        self._srate_updated_at = 0.0

        # FSM циклов — в собственном потоке.
        self.engine = CycleEngine()

        # Гейтинг: контекст поддержания задаёт GUI (таймер 100 мс
        # пишет атомарные bool/float — ГИЛ делает присваивание
        # атомарным, расхождение максимум на один кадр).
        self._gate_maintain_active = False
        self._gate_target_n = None
        self._gate_tol_n = 0.1
        self._gate_window_mean = None

        # Фактическая скорость траверсы: d(mm)/dt по кадрам,
        # усреднение ~1 с (алгоритм из process_measurement_frame).
        self.speed_samples = []
        self._measured_speed = None

        # Батчинг.
        self._pending_frames = []
        self._pending_texts = []
        self._pending_bytes = 0
        self._last_emit = time.monotonic()

    # ----------------------------------------------------
    # Управление из GUI (атомарные присваивания — безопасно).
    # ----------------------------------------------------

    def set_history_points(self, max_points):
        # Лимит истории в точках (120 с × фактический SPS);
        # сам SPS уточняется здесь по span — как в add_frame.
        self._max_points = max(1200, int(max_points))

    def set_cycles_enabled(self, enabled):
        self.engine.set_cycles_enabled(enabled)

    def set_traverse_speed(self, speed_mm_s):
        self.engine.set_traverse_speed(speed_mm_s)

    def set_force_stop_active(self, active):
        self.engine._force_stop_active = bool(active)

    def set_maintain_gate(self, active, target_n, tol_n,
                          window_mean):
        # Контекст гейта циклов из GUI (таймер поддержания):
        # active/target/допуск/среднее окна 5 с. None — нет данных.
        self._gate_maintain_active = bool(active)
        self._gate_target_n = target_n
        self._gate_tol_n = tol_n
        self._gate_window_mean = window_mean

    def reset_stream(self):
        # Новый старт измерения: буферы и FSM с нуля
        # (счётчик циклов НЕ сбрасываем — см. task: сброс
        # только по кнопке «СБРОС»; GUI синхронизирует счётчик
        # через set_cycle_count).
        self._rx.clear()
        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()
        self.frame_times.clear()
        self.frame_positions.clear()
        self._pending_frames = []
        self._pending_texts = []
        self._pending_bytes = 0
        self.engine.reset_cycle_analysis()
        self._srate_updated_at = 0.0

    def set_cycle_count(self, count):
        # Ручная установка счётчика (Настройки) и сохранение
        # через clear() — GUI после reset_stream.
        self.engine.cycle_count = int(count)

    # ----------------------------------------------------
    # Поток: минимальный run — живём, пока не остановят.
    # Вся работа выполняется в слоте feed (queued-вызовы
    # из потока SerialWorker попадают сюда же).
    # ----------------------------------------------------

    def run(self):
        # Обработчик событий Qt: queued-сигналы data_received
        # доставляются в feed, stop-вызовы — в shutdown.
        self.exec_()

    def shutdown(self):
        self._stop = True
        self.quit()
        if not self.wait(2000):
            logger.warning("DataPipeline: не завершился за 2000 мс")

    # ----------------------------------------------------
    # Вход: сырые байты (queued из SerialWorker).
    # ----------------------------------------------------

    def feed(self, data):
        if not data:
            return

        self._rx.extend(data)

        if len(self._rx) > RX_BUFFER_LIMIT:
            del self._rx[:len(self._rx) - RX_BUFFER_LIMIT]

        off = 0
        n = len(self._rx)
        buf = self._rx

        while off < n:

            if buf[off] == FRAME_START:

                if n - off < FRAME_SIZE:
                    # Неполный кадр — ждём следующий чанк.
                    break

                if buf[off + FRAME_SIZE - 1] == FRAME_END:
                    try:
                        (_start, raw, filtered, force_n,
                         current_mm, _end) = struct.unpack(
                            FRAME_FORMAT,
                            bytes(buf[off:off + FRAME_SIZE]),
                        )
                        self._on_frame(
                            raw, filtered, force_n, current_mm
                        )
                    except Exception:
                        # Битый кадр считаем потерей синхронизации
                        # (как GUI-путь: сдвиг на 1 байт).
                        logger.exception(
                            "PIPELINE: ошибка разбора кадра"
                        )

                    off += FRAME_SIZE
                    continue

                # Потеря синхронизации: сдвиг на 1 байт.
                off += 1
                continue

            lf_index = buf.find(b"\n", off)

            if lf_index >= 0:
                self._pending_texts.append(
                    bytes(buf[off:lf_index + 1])
                )
                off = lf_index + 1
                continue

            aa_index = buf.find(bytes([FRAME_START]), off)

            if aa_index > off:
                self._pending_texts.append(bytes(buf[off:aa_index]))
                off = aa_index
                continue

            # Хвост без '\n' и без 0xAA — до следующего чанка.
            break

        if off:
            del self._rx[:off]

        # Батчинг: по времени или по объёму.
        now = time.monotonic()
        if (
                self._pending_frames
                or self._pending_texts
        ) and (
                self._pending_bytes >= EMIT_THRESHOLD_BYTES
                or (now - self._last_emit) >= EMIT_INTERVAL_S
        ):
            self._emit_batch()

    # ----------------------------------------------------
    # Обработка кадра: буферы + FSM.
    # ----------------------------------------------------

    def _on_frame(self, raw, filtered, force_n, current_mm):
        self.raw_values.append(raw)
        self.filtered_values.append(filtered)
        self.values.append(force_n)
        self.frame_times.append(time.time())

        try:
            self.frame_positions.append(
                float(current_mm)
                if current_mm is not None
                else None
            )
        except (TypeError, ValueError):
            self.frame_positions.append(None)

        self._pending_frames.append(
            (raw, filtered, force_n, current_mm)
        )
        self._pending_bytes += FRAME_SIZE

        # ----------------------------------------------------
        # Скорость траверсы: d(mm)/dt по кадрам за ~1 с
        # (перенос алгоритма из process_measurement_frame).
        # ----------------------------------------------------
        now_t = self.frame_times[-1]

        if current_mm is not None:
            try:
                self.speed_samples.append(
                    (now_t, float(current_mm))
                )
            except (TypeError, ValueError):
                pass

        speed_cutoff = now_t - 1.0

        while (
                self.speed_samples
                and self.speed_samples[0][0] < speed_cutoff
        ):
            del self.speed_samples[0]

        if len(self.speed_samples) >= 2:
            t0, mm0 = self.speed_samples[0]
            t1, mm1 = self.speed_samples[-1]
            span = t1 - t0
            if span >= 0.05:
                self._measured_speed = (mm1 - mm0) / span

        # ----------------------------------------------------
        # Гейтинг циклов — та же семантика, что в
        # process_measurement_frame: с поддержанием — среднее
        # окна в допуске от цели; без — нет макро-движения.
        # ----------------------------------------------------
        if (
                self._gate_maintain_active
                and self._gate_target_n is not None
        ):
            cycles_ok = (
                    self._gate_window_mean is not None
                    and abs(
                        self._gate_window_mean
                        - self._gate_target_n
                    ) <= self._gate_tol_n
            )
        else:
            cycles_ok = (
                    self._measured_speed is None
                    or abs(self._measured_speed)
                    <= self.engine.macro_move_speed
            )

        self.engine.set_cycles_enabled(cycles_ok)
        self.engine.set_traverse_speed(self._measured_speed)

        # FSM — в этом потоке.
        self.engine.process_frame(force_n)

        # Адаптивный sample_rate (span, не чаще раза в 2 с) —
        # та же логика, что в add_frame.
        now_s = self.frame_times[-1]
        count = len(self.frame_times)
        if (
                now_s - self._srate_updated_at > 2.0
                and count >= 64
        ):
            span = self.frame_times[-1] - self.frame_times[0]
            if span > 0:
                cand = (count - 1) / span
                if 100.0 <= cand <= 5000.0:
                    self._srate = cand
                    self._max_points = max(
                        1200, int(cand * 120)
                    )
                    self._srate_updated_at = now_s

        # Обрезка истории — чанком (как в add_frame).
        if len(self.values) > self._max_points + TRIM_CHUNK:
            remove_count = len(self.values) - self._max_points
            del self.values[:remove_count]
            del self.raw_values[:remove_count]
            del self.filtered_values[:remove_count]
            del self.frame_times[:remove_count]
            del self.frame_positions[:remove_count]

    # ----------------------------------------------------
    # Выход: батч в GUI.
    # ----------------------------------------------------

    def _emit_batch(self):
        frames = self._pending_frames
        texts = self._pending_texts
        self._pending_frames = []
        self._pending_texts = []
        self._pending_bytes = 0
        self._last_emit = time.monotonic()

        eng = self.engine
        snapshot = {
            "cycle_count": eng.cycle_count,
            "cycle_max": eng.cycle_max,
            "cycle_min": eng.cycle_min,
            "cycle_mid": eng.cycle_mid,
            "cycle_amplitude": eng.cycle_amplitude,
            "cycle_mid_display": eng.cycle_mid_display,
            "cycle_max_value": eng.cycle_max_value,
            "cycle_min_value": eng.cycle_min_value,
            "cycles_evaluating": eng.cycles_evaluating,
            "macro_moving": eng.macro_moving,
        }

        self.batch_ready.emit(frames, texts, snapshot)
