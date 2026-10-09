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

        while self.rx_buffer:

            if (
                    self.rx_buffer[0]
                    == FRAME_START
            ):

                if (
                        len(self.rx_buffer)
                        < MEASUREMENT_FRAME_SIZE
                ):
                    return

                if (
                        self.rx_buffer[
                            MEASUREMENT_FRAME_SIZE - 1
                        ]
                        == FRAME_END
                ):
                    frame = bytes(
                        self.rx_buffer[
                        :MEASUREMENT_FRAME_SIZE
                        ]
                    )

                    del self.rx_buffer[
                        :MEASUREMENT_FRAME_SIZE
                        ]

                    self.process_measurement_frame(
                        frame
                    )

                    continue

                del self.rx_buffer[0]

                continue

            lf_index = self.rx_buffer.find(
                b"\n"
            )

            if lf_index >= 0:
                text_bytes = bytes(
                    self.rx_buffer[
                    :lf_index + 1
                    ]
                )

                del self.rx_buffer[
                    :lf_index + 1
                    ]

                self.process_text_bytes(
                    text_bytes
                )

                continue

            aa_index = self.rx_buffer.find(
                bytes([FRAME_START])
            )

            if aa_index > 0:
                text_bytes = bytes(
                    self.rx_buffer[
                    :aa_index
                    ]
                )

                del self.rx_buffer[
                    :aa_index
                    ]

                self.process_text_bytes(
                    text_bytes
                )

                continue

            return

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

        logger.debug(f"RCV FRAME: FORCE_N={force_n:.6f} N, CURRENT_MM={current_mm:.3f} mm")

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

