"""SerialProtocolMixin — вынесен из calibration_window.py механическим
сплитом (байт-в-байт), ADR сплита монолита. Импорт-блок
исходного файла сохранён целиком; неиспользуемые импорты
безвредны и чистятся отдельно.
"""

import sys
import time
import math
import struct
import configparser
from pathlib import Path

import serial
import serial.tools.list_ports

from PyQt5.QtCore import Qt, QTimer, QRectF, QSize
from PyQt5.QtGui import QPainter, QPen, QFont, QColor
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QTextEdit,
    QPlainTextEdit,
    QDoubleSpinBox,
    QTabWidget,
    QComboBox,
    QMessageBox,
    QFrame,
    QMenu,
    QLineEdit,
    QCheckBox,
    QSizePolicy,
    QFileDialog,
)

from src.protocol import (
    SERIAL_BAUD,
    SERIAL_TIMEOUT,
    HELLO_COMMAND,
    CAL_ZERO_COMMAND,
    CAL_LOAD_COMMAND,
    CAL_GET_COMMAND,
    CAL_APPLY_COMMAND,
    START_COMMAND,
    STOP_COMMAND,
    HOME_COMMAND,
    MEASUREMENT_FRAME_FORMAT,
    MEASUREMENT_FRAME_SIZE,
    FRAME_START,
    FRAME_END,
    GRAVITY,
    INI_PATH,
    APP_SETTINGS_PATH,
    REPO_ROOT,
    USE_DATA_PIPELINE,
)
from src.graph_widget import ForceGraphWidget
from src.serial_worker import SerialWorker, WORKER_READ_TIMEOUT_S
from src.session_recorder import (
    SessionRecorder,
    parse_interval,
    format_interval_hint,
)
from src.logging_setup import logger



class SerialProtocolMixin:

    # Предел роста rx_buffer (байт): при потере синхронизации
    # буфер раньше мог расти неограниченно; держим только хвост.
    RX_BUFFER_LIMIT = 65536

    def connect_serial(self):

        port = self.port_combo.currentText()

        if not port:
            QMessageBox.warning(
                self,
                "Ошибка",
                "COM-порт не выбран."
            )

            return

        try:

            self.serial = serial.Serial(
                port=port,
                baudrate=SERIAL_BAUD,
                timeout=SERIAL_TIMEOUT,
            )

            # Чтение/запись порта — в отдельном потоке, чтобы
            # GUI не блокировался (раньше QTimer poll_serial
            # каждые 5 мс делал read + parse в GUI-потоке).
            self.start_serial_worker()

            self.connected = True

            self.connect_button.setText(
                "Отключить"
            )

            self.connection_status.setText(
                f"Подключено: {port}"
            )

            self.connection_status.setStyleSheet(
                "color: #7CFFB2; "
                "font-weight: bold;"
            )

            self.append_log(
                f"CONNECTED: {port}"
            )
            logger.info(f"CONNECTED: {port}")

            self.start_button.setEnabled(
                True
            )

            self.stop_button.setEnabled(
                False
            )

            self.send_command(
                HELLO_COMMAND
            )

            # Выход траверсы на стартовую позицию
            # (настройки: позиция и скорость).
            self.move_traverse_to_start_position()

        except Exception as e:

            self.serial = None
            self.connected = False

            self.append_log(
                f"ERROR CONNECT: {e}"
            )

            QMessageBox.critical(
                self,
                "Ошибка подключения",
                str(e),
            )

    def disconnect_serial(self):

        # Перед закрытием порта — остановить всё на устройстве
        # (кнопка «Отключить» тоже останавливает измерение).
        if self.connected and self.serial is not None:
            self.send_command(STOP_COMMAND)

        # Остановить воркер: перед выходом он сбросит очередь TX
        # (STOP выше успеет уйти) и закроет порт сам.
        self.stop_serial_worker()

        if self.serial is not None:

            try:
                self.serial.close()
            except Exception:
                pass

        self.serial = None
        self.connected = False
        logger.info("DISCONNECTED")

        # Потеря подключения: поддержание силы бессмысленно —
        # канала для MOVE нет, а таймер регулятора продолжит
        # тикать и спамить SND FAIL. Останавливаем регулятор и
        # возвращаем управление траверсой пользователю.
        if self.maintain_active:
            self.stop_maintain_force(
                reason="остановлено — нет подключения"
            )
            self.append_log(
                "MAINTAIN: остановлено — нет подключения"
            )

        self.connect_button.setText(
            "Подключить"
        )

        self.connection_status.setText(
            "Не подключено"
        )

        self.connection_status.setStyleSheet(
            "color: #FFB84D; "
            "font-weight: bold;"
        )

        self.start_button.setEnabled(
            False
        )

        self.stop_button.setEnabled(
            False
        )

        self.measurement_running = False

        self.force_graph.stop_measurement()

        self.append_log(
            "DISCONNECTED"
        )

    # ========================================================
    # SERIAL WORKER (поток чтения/записи порта)
    # ========================================================

    def start_serial_worker(self):
        # serial-объект открыт в GUI-потоке; дальше его трогает
        # ТОЛЬКО воркер. Сигналы доставляются в GUI-поток
        # автоматически (queued connection, т.к. воркер — другой
        # поток).
        self.serial_worker = SerialWorker(self.serial)

        if (
                USE_DATA_PIPELINE
                and self.data_pipeline is not None
        ):
            # Байты напрямую в поток-обработчик (queued: читаются
            # и разбираются В ПОТОКЕ pipeline, GUI не задействован).
            self.serial_worker.data_received.connect(
                self.data_pipeline.feed
            )
        else:
            self.serial_worker.data_received.connect(
                self.on_serial_data
            )
        self.serial_worker.tx_done.connect(
            self.on_serial_tx
        )
        self.serial_worker.error.connect(
            self.on_serial_error
        )

        self.serial_worker.start()

    def stop_serial_worker(self):
        worker = getattr(self, "serial_worker", None)

        if worker is not None:
            try:
                worker.stop()
            except Exception as e:
                logger.exception(f"WORKER STOP FAIL: {e}")

            self.serial_worker = None

    # ---- слоты воркера (GUI-поток) -------------------------

    def on_serial_data(self, data):
        # Сырые байты из порта: дописать в буфер и разобрать.
        # Разбор (parse_rx → process_measurement_frame → FSM,
        # карточки, график) — как и прежде, в GUI-потоке.
        try:
            self.rx_buffer.extend(data)

            # Ограничение буфера: при потере синхронизации
            # rx_buffer мог расти безгранично. Держим только
            # хвост (новые байты важнее).
            if len(self.rx_buffer) > self.RX_BUFFER_LIMIT:
                del self.rx_buffer[
                    :len(self.rx_buffer) - self.RX_BUFFER_LIMIT
                ]

            self.parse_rx()

        except Exception as e:
            logger.exception(f"RCV FAIL: {e}")
            self.append_log(f"RX ERROR: {e}")

    def on_pipeline_batch(self, frames, texts, snapshot):
        # Батч из DataPipeline (GUI-поток): кадры уже разобраны,
        # FSM отработана в потоке-обработчике. Здесь только:
        # безопасность (стопор/граница/разрыв), last_*, счётчик
        # кадров, живые экстремумы, текстовые строки устройства
        # и синхронизация состояния циклов в график.
        try:
            for raw, filtered, force_n, current_mm in frames:
                self._process_frame_gui_side(
                    raw, filtered, force_n, current_mm
                )

            for text_bytes in texts:
                self.process_text_bytes(text_bytes)

            self._apply_cycle_snapshot(snapshot)

        except Exception as e:
            logger.exception("PIPELINE BATCH FAIL")
            self.append_log(f"RX ERROR (pipeline): {e}")

    def _process_frame_gui_side(self, raw, filtered, force_n,
                                current_mm):
        # GUI-часть обработки кадра в режиме DataPipeline:
        # всё, что ДОЛЖНО остаться в GUI (безопасность, карточки
        # состояния), без разбора байтов/FSM/скорости (это делает
        # пайплайн).

        self.last_raw = raw
        self.last_filtered = filtered
        self.last_force_n = force_n
        self.last_current_mm = current_mm

        # Стопор по силе: упор в нижний концевик.
        self.check_force_stop(force_n, current_mm)

        # Превышение верхней границы хода траверсы.
        self.check_traverse_limit(force_n, current_mm)

        # Разрыв образца: резкое падение силы до около-нуля.
        self.check_rupture(force_n, time.time())

        self.frame_count += 1

        hex_data = " ".join(
            f"{b:02X}" for b in struct.pack(
                MEASUREMENT_FRAME_FORMAT,
                FRAME_START,
                raw,
                filtered,
                force_n,
                current_mm if current_mm is not None else 0.0,
                FRAME_END,
            )
        )

        self.append_log(
            f"<<< RX [{hex_data}] | "
            f"RAW={raw} | "
            f"FILTERED={filtered} | "
            f"FORCE_N={force_n:.6f} N"
            f"CURRENT_MM={current_mm if current_mm is not None else 0.0:.3f} mm"
        )

        if not self.measurement_running:
            # Без осцилляции карточки МИН/МАКС/СРЕДНЕЕ
            # отслеживают живые экстремумы силы.
            if (
                    self.live_min_force is None
                    or force_n < self.live_min_force
            ):
                self.live_min_force = force_n

            if (
                    self.live_max_force is None
                    or force_n > self.live_max_force
            ):
                self.live_max_force = force_n

    def _apply_cycle_snapshot(self, snapshot):
        # Синхронизация FSM-состояния из пайплайна в график:
        # все потребители (карточки, линии MAX/MIN/MID, регулятор,
        # σ_M → цель) читают атрибуты force_graph — им ничего
        # не нужно знать о пайплайне.
        graph = self.force_graph

        graph.cycle_count = snapshot.get("cycle_count", graph.cycle_count)
        graph.cycle_max = snapshot.get("cycle_max")
        graph.cycle_min = snapshot.get("cycle_min")
        graph.cycle_mid = snapshot.get("cycle_mid")
        graph.cycle_amplitude = snapshot.get("cycle_amplitude")
        graph.cycle_mid_display = snapshot.get("cycle_mid_display")
        graph.cycle_max_value = snapshot.get("cycle_max_value")
        graph.cycle_min_value = snapshot.get("cycle_min_value")

    def on_serial_tx(self, command, ok):
        # Команда записана воркером: лог SND как раньше.
        if ok:
            self.append_log(f">>> TX: {command}")
            logger.debug(f"SND {command}")
        else:
            self.append_log(f"TX ERROR: {command}")

    def on_serial_error(self, message):
        # Ошибка порта — только лог; GUI не роняем.
        logger.error(f"SERIAL: {message}")
        self.append_log(f"RX ERROR: {message}")

    # ========================================================
    # SEND COMMAND
    # ========================================================

    def send_command(self, command):

        if (
                not self.connected
                or self.serial is None
        ):
            logger.debug(f"SND FAIL (нет подключения): {command}")
            self.append_log(
                f"TX ERROR — нет подключения: "
                f"{command}"
            )

            return False

        worker = getattr(self, "serial_worker", None)

        if worker is not None and worker.isRunning():
            # Запись в порт — в воркере (потокобезопасная
            # очередь); лог TX придёт из on_serial_tx.
            worker.enqueue_tx(command)
            return True

        # Запасной путь (воркер не запущен): писать напрямую,
        # как раньше.
        try:

            data = command.encode(
                "ascii"
            )

            self.serial.write(data)
            self.serial.flush()

            self.append_log(
                f">>> TX: {command}"
            )

            logger.debug(f"SND {command}")

            return True

        except Exception as e:

            logger.exception(f"SND FAIL: {e}")
            self.append_log(f"TX ERROR: {e}")

            return False

    # ========================================================
    # SERIAL POLLING (legacy — заменён воркером)
    # ========================================================

    def poll_serial(self):
        # Прежний QTimer-опрос порта каждые 5 мс убран: чтение
        # и запись порта выполняет SerialWorker в отдельном
        # потоке, а байты приходят в GUI слотом on_serial_data.
        # Метод оставлен совместимости ради и НЕ трогает порт
        # (иначе гонка с воркером).
        return

    # ========================================================
    # RX PARSER
    # ========================================================

    def parse_rx(self):
        # Разбор по СМЕЩЕНИЮ (offset), без per-кадровых del префикса.
        #
        # Прежняя версия на каждый кадр/текстовый отрезок делала
        # `del rx_buffer[:k]` — O(размера буфера) memmove на КАЖДЫЙ
        # шаг (при потере синхронизации — на каждый байт). Теперь
        # всё разбирается по локальному off, а буфер сдвигается
        # ОДИН раз в конце (del[:off]) — один memmove на чанк.
        # Семантика разбора (кадры, текст до '\n', текст до 0xAA,
        # ожидание неполного кадра) сохранена байт-в-байт.
        buf = self.rx_buffer
        n = len(buf)
        off = 0

        while off < n:

            if buf[off] == FRAME_START:

                if n - off < MEASUREMENT_FRAME_SIZE:
                    # Неполный кадр — ждём следующий чанк.
                    break

                if (
                        buf[off + MEASUREMENT_FRAME_SIZE - 1]
                        == FRAME_END
                ):
                    frame = bytes(
                        buf[off:off + MEASUREMENT_FRAME_SIZE]
                    )

                    off += MEASUREMENT_FRAME_SIZE

                    self.process_measurement_frame(
                        frame
                    )

                    continue

                # Байт 0xAA, но конец кадра не на позиции 17 —
                # потеря синхронизации: сдвиг на 1 байт.
                off += 1

                continue

            lf_index = buf.find(
                b"\n", off
            )

            if lf_index >= 0:
                text_bytes = bytes(
                    buf[off:lf_index + 1]
                )

                off = lf_index + 1

                self.process_text_bytes(
                    text_bytes
                )

                continue

            aa_index = buf.find(
                bytes([FRAME_START]), off
            )

            if aa_index > off:
                text_bytes = bytes(
                    buf[off:aa_index]
                )

                off = aa_index

                self.process_text_bytes(
                    text_bytes
                )

                continue

            # Хвост без '\n' и без 0xAA — оставить до след.
            # чанка.
            break

        if off:
            del self.rx_buffer[:off]

    # ========================================================
    # TEXT RX
    # ========================================================

    def process_text_bytes(self, data):

        try:

            text = data.decode(
                "ascii",
                errors="replace"
            )

        except Exception:

            text = repr(data)

        text = text.strip(
            "\r\n\0 "
        )

        if not text:
            return

        hex_data = data.hex(
            " "
        ).upper()

        self.append_log(
            f"<<< RX: {text} "
            f"[HEX: {hex_data}]"
        )

        logger.debug(f"RCV {text} [HEX: {hex_data}]")

        self.process_response(
            text
        )

    # ========================================================
    # BINARY RX
    # ========================================================

    def process_measurement_frame(
            self,
            frame
    ):

        try:

            (
                start,
                raw,
                filtered,
                force_n,
                current_mm,
                end,
            ) = struct.unpack(
                MEASUREMENT_FRAME_FORMAT,
                frame
            )

        except struct.error as e:

            self.append_log(
                f"FRAME ERROR: {e}"
            )

            return

        if (
                start != FRAME_START
                or end != FRAME_END
        ):
            return

        # ----------------------------------------------------
        # Валидация полей кадра (P1.3; BUG-18): CRC нет, битая
        # посылка может дать force/mm = inf/NaN или мусор.
        # inf/NaN уводят шкалу Y отрисовки в бесконечный цикл
        # сетки / OverflowError в math.ceil — GUI-фриз на часы.
        # Кадр отбрасываем, разрыв серии — в лог.
        # ----------------------------------------------------
        frame_valid = (
                math.isfinite(force_n)
                and math.isfinite(current_mm)
                and math.isfinite(raw)
                and math.isfinite(filtered)
                and abs(force_n) <= 1.0e4        # датчик ±10 кН — с запасом
                and abs(current_mm) <= 1000.0    # ход ±1000 мм
                and abs(raw) <= 2.0**31          # int32 разумеется
        )

        if not frame_valid:
            self._garbage_frame_count = (
                    getattr(self, "_garbage_frame_count", 0) + 1
            )

            if self._garbage_frame_count <= 5:
                logger.critical(
                    f"RX FRAME GARBAGE (#{self._garbage_frame_count}): "
                    f"FORCE_N={force_n} CURRENT_MM={current_mm} "
                    f"RAW={raw} FILTERED={filtered} — кадр отброшен "
                    f"(CRC нет; проверьте кабель/экранирование)"
                )
                self.append_log(
                    f"битый кадр #{self._garbage_frame_count}: "
                    f"F={force_n}, mm={current_mm} — отброшен"
                )

            return

        self.last_raw = raw
        self.last_filtered = filtered
        self.last_force_n = force_n
        self.last_current_mm = current_mm

        # Стопор по силе: упор в нижний концевик.
        self.check_force_stop(force_n, current_mm)

        # Превышение верхней границы хода траверсы.
        self.check_traverse_limit(force_n, current_mm)

        # Разрыв образца: резкое падение силы до около-нуля.
        self.check_rupture(force_n, time.time())

        # BUG-18 (профилирование): debug-строка на каждый кадр
        # стоила ~9% GUI-ядра (logging + shouldRollover → stat()
        # файловой системы на каждом emit). Удалена: кадры и так
        # видны в hex-журнале ЛОГа (раз в секунду).

        self.frame_count += 1

        # ----------------------------------------------------
        # Актуальная скорость траверсы: d(mm)/dt по кадрам,
        # усреднение за ~1 с (сглаживает квантование
        # CURRENT_MM на устройстве).
        # ----------------------------------------------------

        now = time.time()

        self.speed_samples.append(
            (now, current_mm)
        )

        speed_cutoff = now - 1.0

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
                # Знаковая скорость: для стрелки направления
                # в окне фактической скорости.
                self.measured_speed_mm_s = (
                        (mm1 - mm0)
                        / span
                )

        hex_data = frame.hex(
            " "
        ).upper()

        # BUG-18 (профилирование 29-10-2026): hex-строка в ЛОГ
        # на КАЖДЫЙ кадр при 1000 SPS съедала ~20% GUI-ядра
        # (appendPlainText + ensureCursorVisible). Теперь hex-
        # журнал пишется не чаще раза в секунду.
        now_hex = time.time()

        if (
                now_hex
                - getattr(self, "_last_hex_log_t", 0.0)
                >= 1.0
        ):
            self._last_hex_log_t = now_hex

            self.append_log(
                f"<<< RX [{hex_data}] | "
                f"RAW={raw} | "
                f"FILTERED={filtered} | "
                f"FORCE_N={force_n:.6f} N"
                f"CURRENT_MM={current_mm:.3f} mm"
            )

        # ----------------------------------------------------
        # Показания силы/позиции/счётчика кадров в GUI
        # обновляются таймером update_measurement_info (100 мс)
        # из тех же last_force_n / last_current_mm / frame_count.
        # Прежний per-frame setText дублировал их и вызывал
        # ~170 лишних перерисовок графика в секунду (перелейаут
        # меток → repaint) — замер: 193 → 27 paint/s после
        # снятия. Значения на экране идентичны (10 Гц незаметно).
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Поток данных пишется в график всегда, пока идут
        # кадры (осцилляция может быть выключена — график
        # и показатели продолжают обновляться). Анализ
        # циклов — только во время измерения.
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Порог начала оценки циклов:
        # - поддержание активно: циклы считаются, когда
        #   управляемая величина (среднее окна) в допуске от
        #   цели: ±0.1 Н или ±0.2 МПа (в единицах отображения);
        # - поддержание выключено: циклы считаются, когда нет
        #   макро-перемещения траверсы.
        # ----------------------------------------------------

        if (
                self.maintain_active
                and self.maintain_target_n is not None
        ):
            tol_n = self.CYCLE_START_TOL_N

            if self.force_display_units == "MPa":
                area = self.get_specimen_area_mm2()

                if area is not None:
                    tol_n = self.CYCLE_START_TOL_MPA * area

            if self.maintain_window:
                window_mean = (
                        sum(f for _, f in self.maintain_window)
                        / len(self.maintain_window)
                )

                cycles_ok = (
                        abs(
                            window_mean
                            - self.maintain_target_n
                        )
                        <= tol_n
                )
            else:
                cycles_ok = False
        else:
            cycles_ok = (
                    self.measured_speed_mm_s is None
                    or abs(self.measured_speed_mm_s)
                    <= self.force_graph.macro_move_speed
            )

        self.force_graph.set_cycles_enabled(
            cycles_ok
        )

        # Гейтинг по скорости траверсы (макро-перемещение
        # приостанавливает анализ циклов).
        self.force_graph.set_traverse_speed(
            self.measured_speed_mm_s
        )

        self.force_graph.add_frame(
            raw,
            filtered,
            force_n,
            current_mm,
        )

        if not self.measurement_running:
            # Без осцилляции карточки МИН/МАКС/СРЕДНЕЕ
            # отслеживают живые экстремумы силы.
            if (
                    self.live_min_force is None
                    or force_n < self.live_min_force
            ):
                self.live_min_force = force_n

            if (
                    self.live_max_force is None
                    or force_n > self.live_max_force
            ):
                self.live_max_force = force_n

    # ========================================================
    # STM32 RESPONSES
    # ========================================================

    def process_response(self, line):

        if line == "START_OK_YYY":
            self.append_log(
                "STM32: START confirmed"
            )

            self.measurement_running = True

            self.measurement_start_time = (
                time.time()
            )

            self.frame_count = 0

            self.force_graph.start_measurement()

            self.start_button.setEnabled(
                False
            )

            self.stop_button.setEnabled(
                True
            )

            self.send_command("TARGETN_1.0_YYY")

            return

        if line == "STOP_OK_YYY":
            self.append_log(
                "STM32: STOP confirmed"
            )

            self.measurement_running = False

            self.force_graph.stop_measurement()

            self.start_button.setEnabled(
                self.connected
            )

            self.stop_button.setEnabled(
                False
            )

            return

        if line.startswith(
                "DEVICE_ID_"
        ):
            self.process_device_id(
                line
            )

            return

        if line == "CAL_ZERO_OK_YYY":
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        if line == "CAL_LOAD_OK_YYY":
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        if line == "CAL_NOT_READY_YYY":
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        if line.startswith(
                "CAL_DATA_"
        ):
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            self.process_cal_data(
                line
            )

            return

        if line.startswith(
                "CAL_SET_OK_"
        ):
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        if line.startswith(
                "CAL_SET_ERROR_"
        ):
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        if line.startswith(
                "CAL_APPLY_OK_"
        ):
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        if line.startswith(
                "CAL_APPLY_ERROR_"
        ):
            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        self.append_log(
            f"STM32: {line}"
        )

    # ========================================================
    # DEVICE ID
    # ========================================================

    def process_device_id(self, line):

        try:

            parts = line.split("_")

            if len(parts) < 3:
                return

            device_id = parts[1]

            self.device_id = device_id

            self.device_id_label.setText(
                device_id
            )

            self.device_rx_label.setText(
                line
            )

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            self.append_calibration_log(
                f"STM32 DEVICE ID = "
                f"{device_id}"
            )

            self.append_log(
                f"DEVICE_ID = {device_id}"
            )

            self.load_calibration()

        except Exception as e:

            self.append_log(
                f"DEVICE_ID ERROR: {e}"
            )

            self.append_calibration_log(
                f"DEVICE_ID ERROR: {e}"
            )

    # ========================================================
    # CAL DATA
    # ========================================================

