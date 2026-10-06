import sys
import time
import configparser
from pathlib import Path

import serial
import serial.tools.list_ports

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.0

# Команды отправляются БЕЗ CR/LF.
HELLO_COMMAND = "HELLO_YYY"
CAL_ZERO_COMMAND = "CALZERO_YYY"
CAL_LOAD_COMMAND = "CALLOAD_YYY"
CAL_GET_COMMAND = "CALGET_YYY"

GRAVITY = 0.00980665  # N per gram

INI_PATH = Path(__file__).resolve().with_name("calibration.ini")


class CalibrationWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("STM32 — Калибровка датчика усилия")
        self.resize(900, 650)

        self.serial_port = None
        self.rx_buffer = bytearray()

        # Идентификатор подключённого STM32 (96-bit UID, 24 hex symbols)
        self.device_id = None

        # Полученные от STM32 значения
        self.zero_raw = None
        self.load_raw = None

        # Результаты текущего расчёта
        self.delta = None
        self.gain_g_per_count = None
        self.mass_g = 500.0
        self.force_n = None

        self.build_ui()
        self.ini_state_label.setText("ожидание идентификатора устройства")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_serial)
        self.timer.start(20)

        self.refresh_ports()
        self.update_values()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # --------------------------------------------------------------
        # Connection
        # --------------------------------------------------------------
        connection_group = QGroupBox("ПОДКЛЮЧЕНИЕ")
        connection_layout = QHBoxLayout(connection_group)

        connection_layout.addWidget(QLabel("COM-порт:"))

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(260)
        connection_layout.addWidget(self.port_combo)

        self.refresh_button = QPushButton("ОБНОВИТЬ")
        self.refresh_button.clicked.connect(self.refresh_ports)
        connection_layout.addWidget(self.refresh_button)

        self.connect_button = QPushButton("ПОДКЛЮЧИТЬ")
        self.connect_button.clicked.connect(self.toggle_connection)
        connection_layout.addWidget(self.connect_button)

        connection_layout.addStretch()

        self.status_label = QLabel("Отключено")
        connection_layout.addWidget(self.status_label)

        self.device_state_label = QLabel("Устройство: не определено")
        connection_layout.addWidget(self.device_state_label)

        layout.addWidget(connection_group)

        # --------------------------------------------------------------
        # Calibration procedure
        # --------------------------------------------------------------
        calibration_group = QGroupBox("ПОСЛЕДОВАТЕЛЬНОСТЬ КАЛИБРОВКИ")
        calibration_layout = QGridLayout(calibration_group)
        calibration_layout.setHorizontalSpacing(10)
        calibration_layout.setVerticalSpacing(8)

        calibration_layout.addWidget(QLabel("Эталонная масса, г:"), 0, 0)

        self.mass_spin = QDoubleSpinBox()
        self.mass_spin.setRange(0.001, 100000.0)
        self.mass_spin.setDecimals(3)
        self.mass_spin.setSingleStep(1.0)
        self.mass_spin.setValue(500.0)
        self.mass_spin.valueChanged.connect(self.on_mass_changed)
        calibration_layout.addWidget(self.mass_spin, 0, 1)

        self.zero_button = QPushButton("1. НУЛЬ")
        self.zero_button.clicked.connect(self.send_zero)
        calibration_layout.addWidget(self.zero_button, 1, 0)

        self.load_button = QPushButton("2. ГРУЗ")
        self.load_button.clicked.connect(self.send_load)
        calibration_layout.addWidget(self.load_button, 1, 1)

        self.get_button = QPushButton("3. CALGET / РАСЧЁТ")
        self.get_button.clicked.connect(self.send_get)
        calibration_layout.addWidget(self.get_button, 1, 2)

        self.set_button = QPushButton("4. CALSET")
        self.set_button.clicked.connect(self.send_set)
        calibration_layout.addWidget(self.set_button, 1, 3)

        self.ini_label = QLabel(f"INI: {INI_PATH.name}")
        calibration_layout.addWidget(self.ini_label, 2, 0, 1, 4)

        layout.addWidget(calibration_group)

        # --------------------------------------------------------------
        # Values
        # --------------------------------------------------------------
        values_group = QGroupBox("РЕЗУЛЬТАТЫ КАЛИБРОВКИ")
        values_layout = QGridLayout(values_group)
        values_layout.setHorizontalSpacing(30)
        values_layout.setVerticalSpacing(8)

        values_layout.addWidget(QLabel("ZERO_VALUE:"), 0, 0)
        self.zero_label = QLabel("—")
        values_layout.addWidget(self.zero_label, 0, 1)

        values_layout.addWidget(QLabel("LOAD_VALUE:"), 1, 0)
        self.load_label = QLabel("—")
        values_layout.addWidget(self.load_label, 1, 1)

        values_layout.addWidget(QLabel("DELTA:"), 2, 0)
        self.delta_label = QLabel("—")
        values_layout.addWidget(self.delta_label, 2, 1)

        values_layout.addWidget(QLabel("MASS, г:"), 3, 0)
        self.mass_label = QLabel("—")
        values_layout.addWidget(self.mass_label, 3, 1)

        values_layout.addWidget(QLabel("FORCE, Н:"), 4, 0)
        self.force_label = QLabel("—")
        values_layout.addWidget(self.force_label, 4, 1)

        values_layout.addWidget(QLabel("GAIN, г/отсчёт:"), 0, 2)
        self.gain_label = QLabel("—")
        values_layout.addWidget(self.gain_label, 0, 3)

        values_layout.addWidget(QLabel("Формула GAIN:"), 1, 2)
        self.gain_formula_label = QLabel("MASS / (LOAD - ZERO)")
        values_layout.addWidget(self.gain_formula_label, 1, 3)

        values_layout.addWidget(QLabel("Формула MASS_G:"), 2, 2)
        self.mass_formula_label = QLabel("(FILTERED - ZERO_VALUE) × GAIN")
        values_layout.addWidget(self.mass_formula_label, 2, 3)

        values_layout.addWidget(QLabel("Формула FORCE_N:"), 3, 2)
        self.force_formula_label = QLabel("MASS_G × 0.00980665")
        values_layout.addWidget(self.force_formula_label, 3, 3)

        values_layout.addWidget(QLabel("INI:"), 4, 2)
        self.ini_state_label = QLabel("не записан")
        values_layout.addWidget(self.ini_state_label, 4, 3)

        values_layout.addWidget(QLabel("CALSET:"), 5, 2)
        self.calset_state_label = QLabel("не отправлен")
        values_layout.addWidget(self.calset_state_label, 5, 3)

        layout.addWidget(values_group)

        # --------------------------------------------------------------
        # Log
        # --------------------------------------------------------------
        log_group = QGroupBox("ЖУРНАЛ")
        log_layout = QVBoxLayout(log_group)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        log_layout.addWidget(self.log)

        layout.addWidget(log_group, stretch=1)

    # ------------------------------------------------------------------
    # Serial connection
    # ------------------------------------------------------------------

    def refresh_ports(self):
        current = self.port_combo.currentData()

        self.port_combo.clear()

        for port in serial.tools.list_ports.comports():
            self.port_combo.addItem(
                f"{port.device} — {port.description}",
                port.device,
            )

        if current is not None:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def toggle_connection(self):
        if self.serial_port is not None and self.serial_port.is_open:
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self):
        port_name = self.port_combo.currentData()

        if not port_name:
            QMessageBox.warning(self, "COM-порт", "Выберите COM-порт.")
            return

        try:
            self.serial_port = serial.Serial(
                port=port_name,
                baudrate=SERIAL_BAUD,
                timeout=SERIAL_TIMEOUT,
            )
            self.rx_buffer.clear()
            self.device_id = None

            # Не показываем старую калибровку до проверки UID.
            self.zero_raw = None
            self.load_raw = None
            self.delta = None
            self.gain_g_per_count = None
            self.force_n = None
            self.update_values()

            self.connect_button.setText("ОТКЛЮЧИТЬ")
            self.status_label.setText(f"Подключено: {port_name}")
            self.device_state_label.setText("Устройство: определение...")
            self.ini_state_label.setText("ожидание идентификатора")
            self.calset_state_label.setText("не отправлен")
            self.append_log(f"Подключение: {port_name}")

            # Небольшая пауза после открытия CDC, затем handshake.
            QTimer.singleShot(100, self.request_device_id)

        except Exception as exc:
            self.serial_port = None
            QMessageBox.critical(self, "Ошибка подключения", str(exc))

    def disconnect_serial(self):
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except Exception:
                pass

        self.serial_port = None
        self.rx_buffer.clear()
        self.connect_button.setText("ПОДКЛЮЧИТЬ")
        self.status_label.setText("Отключено")
        self.device_state_label.setText("Устройство: не определено")
        self.append_log("Отключение")

    # ------------------------------------------------------------------
    # Device identification / calibration restore
    # ------------------------------------------------------------------

    def request_device_id(self):
        # Первый обмен после открытия COM-порта.
        self.send_command(
            HELLO_COMMAND,
            "HELLO — запрос аппаратного UID STM32",
        )

    def handle_device_id(self, device_id):
        self.device_id = device_id.upper()

        self.device_state_label.setText(
            f"Устройство: {self.device_id}"
        )

        self.append_log(
            f"STM32 UID: {self.device_id}"
        )

        self.load_device_calibration()

    def load_device_calibration(self):
        """
        Ищет в INI калибровку именно для текущего UID.

        Формат INI намеренно простой: одно текущее устройство.
        Если файла нет, идентификатора нет или UID другой —
        INI полностью очищается, старая калибровка не используется.
        """
        if self.device_id is None:
            return

        if not INI_PATH.exists():
            self.clear_ini_and_require_calibration(
                "INI не найден: проведите калибровку устройства."
            )
            return

        config = configparser.ConfigParser()

        try:
            config.read(INI_PATH, encoding="utf-8")

            if "CALIBRATION" not in config:
                self.clear_ini_and_require_calibration(
                    "В INI нет идентификатора устройства. Проведите калибровку."
                )
                return

            section = config["CALIBRATION"]
            saved_device_id = section.get("device_id", "").strip().upper()

            if not saved_device_id:
                self.clear_ini_and_require_calibration(
                    "В INI нет идентификатора устройства. Проведите калибровку."
                )
                return

            if saved_device_id != self.device_id:
                self.clear_ini_and_require_calibration(
                    "Такого устройства нет в INI. Старые данные удалены; проведите калибровку."
                )
                return

            zero_value = section.getint("zero_value", fallback=None)
            gain = section.getfloat("gain_g_per_count", fallback=None)

            if zero_value is None or gain is None or gain <= 0.0:
                self.clear_ini_and_require_calibration(
                    "В INI нет корректных ZERO/GAIN. Проведите калибровку."
                )
                return

            self.zero_raw = zero_value
            self.gain_g_per_count = gain
            self.load_raw = None
            self.delta = None
            self.force_n = None
            self.update_values()

            self.ini_state_label.setText(
                f"найдено: {INI_PATH.name}"
            )
            self.device_state_label.setText(
                f"Устройство: {self.device_id} — калибровка найдена"
            )

            self.append_log(
                f"INI: найдено устройство {self.device_id}; "
                f"ZERO={self.zero_raw}; GAIN={self.gain_g_per_count:.12g} g/count"
            )

            self.send_calibration_apply()

        except (configparser.Error, ValueError, OSError) as exc:
            self.clear_ini_and_require_calibration(
                f"Ошибка чтения INI: {exc}. Проведите калибровку."
            )

    def clear_ini_and_require_calibration(self, reason):
        """Полностью очищает INI и переводит программу в режим калибровки."""
        try:
            INI_PATH.write_text("", encoding="utf-8")
        except OSError as exc:
            self.append_log(f"ОШИБКА очистки INI: {exc}")

        self.zero_raw = None
        self.load_raw = None
        self.delta = None
        self.gain_g_per_count = None
        self.force_n = None
        self.update_values()

        self.ini_state_label.setText("очищен — требуется калибровка")
        self.device_state_label.setText(
            "Устройство: нет готовой калибровки — проведите калибровку"
        )
        self.calset_state_label.setText("не отправлен")
        self.append_log(reason)
        self.append_log(
            "Проведите калибровку: 1. НУЛЬ → 2. ГРУЗ → 3. CALGET / РАСЧЁТ → 4. CALSET"
        )

    def send_calibration_apply(self):
        if self.device_id is None:
            return False

        if self.zero_raw is None or self.gain_g_per_count is None:
            return False

        command = (
            f"CALAPPLY_{self.zero_raw}_{self.gain_g_per_count:.12g}_YYY"
        )

        return self.send_command(
            command,
            f"CALAPPLY — ZERO={self.zero_raw}, "
            f"GAIN={self.gain_g_per_count:.12g} g/count",
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def send_command(self, command, description):
        if self.serial_port is None or not self.serial_port.is_open:
            QMessageBox.warning(self, "USB", "Сначала подключите STM32.")
            return False

        try:
            # ВАЖНО: команды отправляются ровно как определено,
            # без \r и \n.
            payload = command.encode("ascii")
            self.serial_port.write(payload)
            self.serial_port.flush()
            self.append_log(f">>> {command}    ({description})")
            return True
        except Exception as exc:
            self.append_log(f"ОШИБКА TX: {exc}")
            return False

    def send_zero(self):
        # 1. НУЛЬ → CALZERO_YYY
        self.send_command(
            CAL_ZERO_COMMAND,
            "CALZERO — STM32 собирает ZERO_VALUE",
        )

    def send_load(self):
        # 2. ГРУЗ → CALLOAD_YYY
        # Масса здесь только отображается для оператора.
        # Она НЕ отправляется STM32.
        self.send_command(
            CAL_LOAD_COMMAND,
            f"CALLOAD — эталон {self.mass_spin.value():.3f} г",
        )

    def send_get(self):
        # 3. CALGET → получить ZERO и LOAD.
        self.send_command(
            CAL_GET_COMMAND,
            "CALGET — получить ZERO_VALUE и LOAD_VALUE; затем выполнить расчёт",
        )

    def send_set(self):
        # 4. CALSET → отправить рассчитанный коэффициент.
        if self.gain_g_per_count is None:
            QMessageBox.warning(
                self,
                "CALSET",
                "Сначала нажмите 3. CALGET / РАСЧЁТ и получите коэффициент.",
            )
            return

        command = f"CALSET_{self.gain_g_per_count:.12g}_YYY"

        if self.send_command(
            command,
            f"CALSET — GAIN={self.gain_g_per_count:.12g} g/count",
        ):
            self.calset_state_label.setText("отправлен")

    # ------------------------------------------------------------------
    # RX
    # ------------------------------------------------------------------

    def poll_serial(self):
        if self.serial_port is None or not self.serial_port.is_open:
            return

        try:
            data = self.serial_port.read(4096)
            if data:
                self.rx_buffer.extend(data)
                self.parse_rx()
        except Exception as exc:
            self.append_log(f"ОШИБКА RX: {exc}")
            self.disconnect_serial()

    def parse_rx(self):
        # STM32 может прислать CRLF. Команды от Python при этом
        # всё равно остаются без CR/LF.
        while b"\n" in self.rx_buffer:
            newline_index = self.rx_buffer.find(b"\n")
            raw_line = bytes(self.rx_buffer[:newline_index + 1])
            del self.rx_buffer[:newline_index + 1]

            line = raw_line.decode("ascii", errors="replace").strip()
            if not line:
                continue

            self.append_log(f"<<< {line}")
            self.process_response(line)

    def process_response(self, line):
        # --------------------------------------------------------------
        # Идентификатор STM32
        # DEVICE_ID_<24 HEX symbols>_YYY
        # --------------------------------------------------------------
        if line.startswith("DEVICE_ID_") and line.endswith("_YYY"):
            parts = line.split("_")

            if len(parts) != 4 or parts[0] != "DEVICE" or parts[1] != "ID" or parts[3] != "YYY":
                self.append_log(
                    f"ОШИБКА: неверный формат DEVICE_ID: {parts!r}"
                )
                return

            device_id = parts[2].strip().upper()

            if len(device_id) != 24:
                self.append_log(
                    f"ОШИБКА: неверная длина DEVICE_ID: {device_id!r}"
                )
                return

            try:
                int(device_id, 16)
            except ValueError:
                self.append_log(
                    f"ОШИБКА: DEVICE_ID содержит не HEX: {device_id!r}"
                )
                return

            self.handle_device_id(device_id)
            return

        if line == "CAL_APPLY_OK_YYY":
            self.calset_state_label.setText("калибровка загружена в STM32")
            self.device_state_label.setText(
                f"Устройство: {self.device_id} — калибровка загружена"
            )
            self.append_log("STM32 подтвердил CALAPPLY.")
            return

        if line == "CAL_APPLY_ERROR_YYY":
            self.calset_state_label.setText("ошибка загрузки калибровки")
            self.device_state_label.setText(
                f"Устройство: {self.device_id} — ошибка CALAPPLY"
            )
            self.append_log("STM32 вернул ошибку CALAPPLY.")
            return

        # --------------------------------------------------------------
        # CALGET
        # --------------------------------------------------------------
        # Ожидаемый ответ STM32:
        # CAL_DATA_<ZERO_VALUE>_<LOAD_VALUE>_YYY
        if line.startswith("CAL_DATA_") and line.endswith("_YYY"):
            parts = line.split("_")

            # Должно быть ровно:
            # ["CAL", "DATA", "ZERO", "LOAD", "YYY"]
            if (
                len(parts) != 5
                or parts[0] != "CAL"
                or parts[1] != "DATA"
                or parts[4] != "YYY"
            ):
                self.append_log(
                    f"ОШИБКА: неверный формат CAL_DATA: {parts!r}"
                )
                return

            try:
                zero_value = int(parts[2])
                load_value = int(parts[3])
            except ValueError:
                self.append_log(
                    "ОШИБКА: ZERO_VALUE / LOAD_VALUE не являются числами"
                )
                return

            self.zero_raw = zero_value
            self.load_raw = load_value

            self.append_log(
                f"CALGET получен: ZERO_VALUE={self.zero_raw}, "
                f"LOAD_VALUE={self.load_raw}"
            )

            # Расчёт ВСЕГДА использует текущее значение поля массы.
            # Старое значение из INI здесь не используется.
            self.calculate_calibration_and_save_ini()
            return

        if line == "CAL_NOT_READY_YYY":
            self.append_log(
                "STM32: CAL_NOT_READY — ZERO/LOAD ещё не готовы."
            )
            return

        if line == "CAL_SET_OK_YYY":
            self.calset_state_label.setText("OK — записан в STM32")
            self.append_log("STM32 подтвердил CALSET.")
            return

        if line == "CAL_SET_ERROR_YYY":
            self.calset_state_label.setText("ERROR")
            self.append_log("STM32 вернул ошибку CALSET.")
            return

        # Любая другая текстовая строка просто логируется.

    # ------------------------------------------------------------------
    # Calculation + INI
    # ------------------------------------------------------------------

    def on_mass_changed(self, value):
        # Это единственный источник массы для нового расчёта.
        self.mass_g = float(value)
        self.update_values()

    def calculate_calibration_and_save_ini(self):
        if self.zero_raw is None or self.load_raw is None:
            return False

        # ГЛАВНОЕ:
        # берём массу непосредственно из UI в момент CALGET.
        mass_g = float(self.mass_spin.value())
        self.mass_g = mass_g

        # DELTA = LOAD - ZERO
        self.delta = self.load_raw - self.zero_raw

        if self.delta == 0:
            self.gain_g_per_count = None
            self.force_n = None
            self.ini_state_label.setText("ошибка: DELTA = 0")
            self.update_values()

            QMessageBox.critical(
                self,
                "Калибровка",
                "DELTA = 0. ZERO_VALUE и LOAD_VALUE совпадают.",
            )
            return False

        # GAIN = масса / (LOAD - ZERO)
        self.gain_g_per_count = mass_g / float(self.delta)

        # FORCE = масса * g
        self.force_n = mass_g * GRAVITY

        self.update_values()

        self.append_log(
            "РАСЧЁТ: "
            f"MASS={mass_g:.6f} g / "
            f"DELTA={self.delta} -> "
            f"GAIN={self.gain_g_per_count:.12g} g/count"
        )
        self.append_log(
            f"РАСЧЁТ: FORCE={self.force_n:.9f} N"
        )

        return self.save_ini()

    def save_ini(self):
        if (
            self.device_id is None
            or self.zero_raw is None
            or self.gain_g_per_count is None
        ):
            self.ini_state_label.setText(
                "не записан: нет DEVICE_ID / ZERO / GAIN"
            )
            self.append_log(
                "ОШИБКА INI: для записи нужны DEVICE_ID, ZERO и GAIN."
            )
            return False

        # В INI храним только то, что нужно для восстановления
        # калибровки конкретного устройства.
        config = configparser.ConfigParser()
        config["CALIBRATION"] = {
            "device_id": self.device_id,
            "zero_value": str(self.zero_raw),
            "gain_g_per_count": f"{self.gain_g_per_count:.12g}",
        }

        try:
            with INI_PATH.open("w", encoding="utf-8") as file:
                config.write(file)
        except OSError as exc:
            self.ini_state_label.setText("ошибка записи")
            self.append_log(f"ОШИБКА INI: {exc}")
            QMessageBox.critical(
                self,
                "INI",
                f"Не удалось записать {INI_PATH}:\n{exc}",
            )
            return False

        self.ini_state_label.setText(
            f"записан: {INI_PATH.name}"
        )

        self.append_log(
            "INI: сохранены "
            f"DEVICE_ID={self.device_id}; "
            f"ZERO={self.zero_raw}; "
            f"GAIN={self.gain_g_per_count:.12g} g/count"
        )
        return True

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def update_values(self):
        self.zero_label.setText(
            "—" if self.zero_raw is None else str(self.zero_raw)
        )

        self.load_label.setText(
            "—" if self.load_raw is None else str(self.load_raw)
        )

        if self.zero_raw is not None and self.load_raw is not None:
            self.delta = self.load_raw - self.zero_raw
            self.delta_label.setText(str(self.delta))
        else:
            self.delta_label.setText("—")

        self.mass_label.setText(f"{self.mass_g:.3f}")

        if self.force_n is None:
            self.force_label.setText("—")
        else:
            self.force_label.setText(f"{self.force_n:.9f}")

        if self.gain_g_per_count is None:
            self.gain_label.setText("—")
        else:
            self.gain_label.setText(f"{self.gain_g_per_count:.12g}")

    def append_log(self, text):
        timestamp = time.strftime("%H:%M:%S")
        self.log.appendPlainText(f"{timestamp}  {text}")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self.timer.stop()
        self.disconnect_serial()
        event.accept()


def main():
    app = QApplication(sys.argv)

    window = CalibrationWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
