import sys
import time
import struct
import configparser
from pathlib import Path

import serial
import serial.tools.list_ports

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


# ============================================================
# CONSTANTS
# ============================================================

SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.0

# Команды отправляются БЕЗ CR/LF
HELLO_COMMAND = "HELLO_YYY"

CAL_ZERO_COMMAND = "CALZERO_YYY"
CAL_LOAD_COMMAND = "CALLOAD_YYY"
CAL_GET_COMMAND = "CALGET_YYY"

START_COMMAND = "START_YYY"
STOP_COMMAND = "STOP_YYY"

GRAVITY = 0.00980665

INI_PATH = Path(__file__).resolve().with_name("calibration.ini")

# STM32 binary measurement frame:
# AA + int32 RAW + int32 FILTERED + BB
FRAME_FORMAT = "<BiifB"
FRAME_SIZE = struct.calcsize(FRAME_FORMAT)

# Сколько точек реально рисуем.
# Всё полученное можно хранить до этого количества.
MAX_STORAGE = 200000
MAX_DISPLAY = 4000

# Частота обновления GUI
PLOT_UPDATE_MS = 50


# ============================================================
# COLORS — стиль старого осциллографа
# ============================================================

BG_MAIN = "#101214"
BG_PANEL = "#17191C"
BG_GRAPH = "#0B0D0F"

TEXT_MAIN = "#E6E6E6"
TEXT_DIM = "#8E9399"
GRID_COLOR = "#30343A"

RAW_COLOR = "#FFD400"
FILTERED_COLOR = "#00F6FF"
FORCE_COLOR = "#FF5C8A"

GREEN = "#55D187"
RED = "#FF6262"
BUTTON_BG = "#24282D"
BUTTON_HOVER = "#30353B"


# ============================================================
# MATPLOTLIB CANVAS
# ============================================================

class MeasurementCanvas(FigureCanvas):
    def __init__(self, parent=None):
        self.figure = Figure(
            figsize=(10, 6),
            facecolor=BG_GRAPH
        )

        super().__init__(self.figure)

        self.setParent(parent)

        self.ax = self.figure.add_subplot(111)
        self.ax_force = self.ax.twinx()

        self._setup_axes()

        self.raw_line, = self.ax.plot(
            [],
            [],
            color=RAW_COLOR,
            linewidth=1.2,
            label="RAW"
        )

        self.filtered_line, = self.ax.plot(
            [],
            [],
            color=FILTERED_COLOR,
            linewidth=1.5,
            label="FILTERED"
        )

        self.force_line, = self.ax_force.plot(
            [],
            [],
            color=FORCE_COLOR,
            linewidth=1.5,
            label="FORCE_N"
        )

        self.figure.subplots_adjust(
            left=0.07,
            right=0.93,
            top=0.92,
            bottom=0.10
        )

    def _setup_axes(self):
        self.ax.set_facecolor(BG_GRAPH)
        self.ax_force.set_facecolor("none")

        self.ax.tick_params(
            axis="both",
            colors=TEXT_DIM,
            labelsize=9
        )

        self.ax_force.tick_params(
            axis="y",
            colors=FORCE_COLOR,
            labelsize=9
        )

        self.ax.xaxis.label.set_color(TEXT_DIM)
        self.ax.yaxis.label.set_color(TEXT_DIM)
        self.ax_force.yaxis.label.set_color(FORCE_COLOR)

        self.ax.set_xlabel("SAMPLE")
        self.ax.set_ylabel("RAW / FILTERED")
        self.ax_force.set_ylabel("FORCE, N")

        self.ax.grid(
            True,
            color=GRID_COLOR,
            linewidth=0.6,
            alpha=0.8
        )

        for spine in self.ax.spines.values():
            spine.set_color(GRID_COLOR)

        for spine in self.ax_force.spines.values():
            spine.set_color(GRID_COLOR)

        self.ax_force.spines["right"].set_color(FORCE_COLOR)

    def clear_plot(self):
        self.raw_line.set_data([], [])
        self.filtered_line.set_data([], [])
        self.force_line.set_data([], [])

        self.ax.set_xlim(0, 1)
        self.ax_force.set_ylim(0, 1)

        self.draw_idle()

    def update_plot(self, raw, filtered, force):
        if not raw:
            self.clear_plot()
            return

        count = len(raw)

        start = max(0, count - MAX_DISPLAY)

        raw_view = raw[start:]
        filtered_view = filtered[start:]
        force_view = force[start:]

        x = list(range(start, count))

        self.raw_line.set_data(x, raw_view)
        self.filtered_line.set_data(x, filtered_view)
        self.force_line.set_data(x, force_view)

        # ----------------------------------------------------
        # X
        # ----------------------------------------------------

        if len(x) == 1:
            self.ax.set_xlim(
                max(0, x[0] - 1),
                x[0] + 1
            )
        else:
            self.ax.set_xlim(x[0], x[-1])

        # ----------------------------------------------------
        # LEFT Y — RAW / FILTERED
        # ----------------------------------------------------

        left_values = list(raw_view) + list(filtered_view)

        if left_values:
            ymin = min(left_values)
            ymax = max(left_values)

            if ymin == ymax:
                margin = max(abs(ymin) * 0.05, 1.0)
            else:
                margin = (ymax - ymin) * 0.10

            self.ax.set_ylim(
                ymin - margin,
                ymax + margin
            )

        # ----------------------------------------------------
        # RIGHT Y — FORCE
        # ----------------------------------------------------

        if force_view:
            fmin = min(force_view)
            fmax = max(force_view)

            if fmin == fmax:
                margin = max(abs(fmin) * 0.10, 0.1)
            else:
                margin = (fmax - fmin) * 0.10

            self.ax_force.set_ylim(
                fmin - margin,
                fmax + margin
            )

        self.draw_idle()


# ============================================================
# MAIN WINDOW
# ============================================================

class CalibrationWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "STM32 ADS1220 — Калибровка / Измерения"
        )

        self.resize(1200, 760)

        # ----------------------------------------------------
        # SERIAL
        # ----------------------------------------------------

        self.serial_port = None
        self.rx_buffer = bytearray()

        # ----------------------------------------------------
        # DEVICE
        # ----------------------------------------------------

        self.device_id = ""

        # ----------------------------------------------------
        # CALIBRATION
        # ----------------------------------------------------

        self.zero_raw = None
        self.load_raw = None

        self.delta = None
        self.gain_g_per_count = None

        self.mass_g = 500.0
        self.force_n = None

        self.calibration_applied = False
        self.calset_ok = False

        # ----------------------------------------------------
        # MEASUREMENT
        # ----------------------------------------------------

        self.measurement_active = False

        self.start_pending = False
        self.stop_pending = False

        self.raw_data = []
        self.filtered_data = []
        self.force_data = []

        # ----------------------------------------------------
        # UI
        # ----------------------------------------------------

        self.build_ui()

        # ----------------------------------------------------
        # TIMER
        # ----------------------------------------------------

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.process_serial)
        self.timer.start(10)

        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self.update_measurement_plot)
        self.plot_timer.start(PLOT_UPDATE_MS)

        self.refresh_ports()

    # ========================================================
    # UI
    # ========================================================

    def build_ui(self):

        self.setStyleSheet(
            f"""
            QMainWindow {{
                background: {BG_MAIN};
            }}

            QWidget {{
                background: {BG_MAIN};
                color: {TEXT_MAIN};
                font-size: 10pt;
            }}

            QGroupBox {{
                background: {BG_PANEL};
                border: 1px solid #292D32;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
            }}

            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: {TEXT_MAIN};
            }}

            QPushButton {{
                background: {BUTTON_BG};
                color: {TEXT_MAIN};
                border: 1px solid #383D43;
                border-radius: 4px;
                padding: 7px 12px;
            }}

            QPushButton:hover {{
                background: {BUTTON_HOVER};
            }}

            QPushButton:disabled {{
                color: #555A60;
                background: #181A1D;
            }}

            QComboBox,
            QDoubleSpinBox {{
                background: #0F1113;
                color: {TEXT_MAIN};
                border: 1px solid #383D43;
                border-radius: 3px;
                padding: 5px;
            }}

            QPlainTextEdit {{
                background: #0B0D0F;
                color: #AEB4BA;
                border: 1px solid #292D32;
                font-family: Consolas;
                font-size: 9pt;
            }}

            QTabWidget::pane {{
                border: 1px solid #292D32;
                background: {BG_MAIN};
            }}

            QTabBar::tab {{
                background: #191C20;
                color: #858B91;
                padding: 9px 25px;
                border: 1px solid #292D32;
            }}

            QTabBar::tab:selected {{
                background: #272B30;
                color: white;
            }}

            QLabel {{
                color: {TEXT_MAIN};
            }}
            """
        )

        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # ----------------------------------------------------
        # TOP CONNECTION PANEL
        # ----------------------------------------------------

        connection_box = QGroupBox("ПОДКЛЮЧЕНИЕ")

        connection_layout = QHBoxLayout()

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(130)

        self.refresh_button = QPushButton("ОБНОВИТЬ")

        self.connect_button = QPushButton("ПОДКЛЮЧИТЬ")

        self.connection_status = QLabel("ОТКЛЮЧЕНО")
        self.connection_status.setStyleSheet(
            f"color: {RED}; font-weight: bold;"
        )

        self.device_state = QLabel("DEVICE: ---")
        self.device_state.setStyleSheet(
            f"color: {TEXT_DIM};"
        )

        connection_layout.addWidget(
            QLabel("ПОРТ:")
        )
        connection_layout.addWidget(
            self.port_combo
        )
        connection_layout.addWidget(
            self.refresh_button
        )
        connection_layout.addWidget(
            self.connect_button
        )
        connection_layout.addSpacing(20)
        connection_layout.addWidget(
            self.connection_status
        )
        connection_layout.addSpacing(20)
        connection_layout.addWidget(
            self.device_state
        )
        connection_layout.addStretch()

        connection_box.setLayout(connection_layout)

        main_layout.addWidget(connection_box)

        self.refresh_button.clicked.connect(
            self.refresh_ports
        )

        self.connect_button.clicked.connect(
            self.toggle_connection
        )

        # ----------------------------------------------------
        # TABS
        # ----------------------------------------------------

        self.tabs = QTabWidget()

        self.calibration_tab = self.build_calibration_tab()
        self.measurement_tab = self.build_measurement_tab()

        self.tabs.addTab(
            self.calibration_tab,
            "КАЛИБРОВКА"
        )

        self.tabs.addTab(
            self.measurement_tab,
            "ИЗМЕРЕНИЯ"
        )

        main_layout.addWidget(self.tabs, 1)

        # ----------------------------------------------------
        # LOG
        # ----------------------------------------------------

        log_box = QGroupBox("ЖУРНАЛ")

        log_layout = QVBoxLayout()

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setMinimumHeight(120)

        log_layout.addWidget(self.log)

        log_box.setLayout(log_layout)

        main_layout.addWidget(log_box)

    # ========================================================
    # CALIBRATION TAB
    # ========================================================

    def build_calibration_tab(self):

        widget = QWidget()

        layout = QVBoxLayout(widget)

        # ----------------------------------------------------
        # PROCEDURE
        # ----------------------------------------------------

        procedure_box = QGroupBox(
            "ПРОЦЕДУРА КАЛИБРОВКИ"
        )

        procedure_layout = QGridLayout()

        procedure_layout.addWidget(
            QLabel("КАЛИБРОВОЧНАЯ МАССА, г:"),
            0,
            0
        )

        self.mass_spin = QDoubleSpinBox()
        self.mass_spin.setRange(0.001, 100000.0)
        self.mass_spin.setDecimals(3)
        self.mass_spin.setValue(500.0)

        self.mass_spin.valueChanged.connect(
            self.mass_changed
        )

        procedure_layout.addWidget(
            self.mass_spin,
            0,
            1
        )

        self.cal_zero_button = QPushButton(
            "1. НУЛЬ"
        )

        self.cal_load_button = QPushButton(
            "2. ГРУЗ"
        )

        self.cal_get_button = QPushButton(
            "3. CALGET / РАСЧЁТ"
        )

        self.cal_set_button = QPushButton(
            "4. CALSET"
        )

        procedure_layout.addWidget(
            self.cal_zero_button,
            1,
            0
        )

        procedure_layout.addWidget(
            self.cal_load_button,
            1,
            1
        )

        procedure_layout.addWidget(
            self.cal_get_button,
            2,
            0
        )

        procedure_layout.addWidget(
            self.cal_set_button,
            2,
            1
        )

        self.ini_label = QLabel(
            "INI: НЕТ"
        )

        self.ini_label.setStyleSheet(
            f"color: {TEXT_DIM};"
        )

        procedure_layout.addWidget(
            self.ini_label,
            3,
            0,
            1,
            2
        )

        procedure_box.setLayout(
            procedure_layout
        )

        layout.addWidget(procedure_box)

        # ----------------------------------------------------
        # VALUES
        # ----------------------------------------------------

        values_box = QGroupBox(
            "РЕЗУЛЬТАТЫ"
        )

        values_layout = QGridLayout()

        self.zero_label = self.make_value_label()
        self.load_label = self.make_value_label()
        self.delta_label = self.make_value_label()
        self.mass_label = self.make_value_label()
        self.force_label = self.make_value_label()
        self.gain_label = self.make_value_label()

        self.calset_state_label = self.make_value_label()

        values_layout.addWidget(
            QLabel("ZERO_VALUE:"),
            0,
            0
        )
        values_layout.addWidget(
            self.zero_label,
            0,
            1
        )

        values_layout.addWidget(
            QLabel("LOAD_VALUE:"),
            1,
            0
        )
        values_layout.addWidget(
            self.load_label,
            1,
            1
        )

        values_layout.addWidget(
            QLabel("DELTA:"),
            2,
            0
        )
        values_layout.addWidget(
            self.delta_label,
            2,
            1
        )

        values_layout.addWidget(
            QLabel("MASS:"),
            3,
            0
        )
        values_layout.addWidget(
            self.mass_label,
            3,
            1
        )

        values_layout.addWidget(
            QLabel("FORCE:"),
            4,
            0
        )
        values_layout.addWidget(
            self.force_label,
            4,
            1
        )

        values_layout.addWidget(
            QLabel("GAIN, g/count:"),
            5,
            0
        )
        values_layout.addWidget(
            self.gain_label,
            5,
            1
        )

        values_layout.addWidget(
            QLabel("CALSET:"),
            6,
            0
        )
        values_layout.addWidget(
            self.calset_state_label,
            6,
            1
        )

        values_box.setLayout(
            values_layout
        )

        layout.addWidget(values_box)

        # ----------------------------------------------------
        # FORMULAS
        # ----------------------------------------------------

        formula_box = QGroupBox(
            "РАСЧЁТ"
        )

        formula_layout = QVBoxLayout()

        formula_layout.addWidget(
            QLabel(
                "GAIN = MASS / (LOAD_VALUE - ZERO_VALUE)"
            )
        )

        formula_layout.addWidget(
            QLabel(
                "MASS = (FILTERED - ZERO_VALUE) × GAIN"
            )
        )

        formula_layout.addWidget(
            QLabel(
                "FORCE_N = MASS_G × 0.00980665"
            )
        )

        formula_box.setLayout(
            formula_layout
        )

        layout.addWidget(formula_box)

        layout.addStretch()

        # ----------------------------------------------------
        # CONNECTIONS
        # ----------------------------------------------------

        self.cal_zero_button.clicked.connect(
            self.send_cal_zero
        )

        self.cal_load_button.clicked.connect(
            self.send_cal_load
        )

        self.cal_get_button.clicked.connect(
            self.send_cal_get
        )

        self.cal_set_button.clicked.connect(
            self.send_cal_set
        )

        return widget

    # ========================================================
    # MEASUREMENT TAB
    # ========================================================

    def build_measurement_tab(self):

        widget = QWidget()

        layout = QVBoxLayout(widget)

        # ----------------------------------------------------
        # TEST PARAMETERS / CONTROL
        # ----------------------------------------------------

        control_box = QGroupBox(
            "ИЗМЕРЕНИЕ"
        )

        control_layout = QHBoxLayout()

        self.start_button = QPushButton(
            "▶  START"
        )

        self.stop_button = QPushButton(
            "■  STOP"
        )

        self.start_button.setMinimumWidth(150)
        self.stop_button.setMinimumWidth(150)

        self.stop_button.setEnabled(False)

        self.measurement_status = QLabel(
            "STOPPED"
        )

        self.measurement_status.setStyleSheet(
            f"color: {RED}; font-weight: bold;"
        )

        self.samples_label = QLabel(
            "SAMPLES: 0"
        )

        control_layout.addWidget(
            self.start_button
        )

        control_layout.addWidget(
            self.stop_button
        )

        control_layout.addSpacing(20)

        control_layout.addWidget(
            self.measurement_status
        )

        control_layout.addSpacing(20)

        control_layout.addWidget(
            self.samples_label
        )

        control_layout.addStretch()

        control_box.setLayout(
            control_layout
        )

        layout.addWidget(control_box)

        # ----------------------------------------------------
        # GRAPH
        # ----------------------------------------------------

        self.canvas = MeasurementCanvas(
            widget
        )

        layout.addWidget(
            self.canvas,
            1
        )

        # ----------------------------------------------------
        # BUTTONS
        # ----------------------------------------------------

        self.start_button.clicked.connect(
            self.start_measurement
        )

        self.stop_button.clicked.connect(
            self.stop_measurement
        )

        return widget

    # ========================================================
    # VALUE LABEL
    # ========================================================

    @staticmethod
    def make_value_label():

        label = QLabel("---")

        label.setStyleSheet(
            """
            QLabel {
                background: #0B0D0F;
                border: 1px solid #292D32;
                padding: 4px 8px;
                font-family: Consolas;
            }
            """
        )

        return label

    # ========================================================
    # SERIAL PORTS
    # ========================================================

    def refresh_ports(self):

        current = self.port_combo.currentText()

        self.port_combo.clear()

        ports = serial.tools.list_ports.comports()

        for port in ports:
            self.port_combo.addItem(
                port.device
            )

        index = self.port_combo.findText(
            current
        )

        if index >= 0:
            self.port_combo.setCurrentIndex(
                index
            )

    def toggle_connection(self):

        if self.serial_port is not None:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self):

        port_name = self.port_combo.currentText()

        if not port_name:
            QMessageBox.warning(
                self,
                "Ошибка",
                "COM-порт не выбран."
            )
            return

        try:
            self.serial_port = serial.Serial(
                port=port_name,
                baudrate=SERIAL_BAUD,
                timeout=SERIAL_TIMEOUT
            )

            self.rx_buffer.clear()

            self.connect_button.setText(
                "ОТКЛЮЧИТЬ"
            )

            self.connection_status.setText(
                "ПОДКЛЮЧЕНО"
            )

            self.connection_status.setStyleSheet(
                f"color: {GREEN}; font-weight: bold;"
            )

            self.log_message(
                f"Подключение: {port_name}"
            )

            QTimer.singleShot(
                100,
                self.request_device_id
            )

        except Exception as e:

            self.serial_port = None

            QMessageBox.critical(
                self,
                "Ошибка подключения",
                str(e)
            )

    def disconnect_device(self):

        try:
            if self.serial_port is not None:
                self.serial_port.close()
        except Exception:
            pass

        self.serial_port = None
        self.rx_buffer.clear()

        self.device_id = ""

        self.connect_button.setText(
            "ПОДКЛЮЧИТЬ"
        )

        self.connection_status.setText(
            "ОТКЛЮЧЕНО"
        )

        self.connection_status.setStyleSheet(
            f"color: {RED}; font-weight: bold;"
        )

        self.device_state.setText(
            "DEVICE: ---"
        )

        self.log_message(
            "Устройство отключено."
        )

    # ========================================================
    # SEND
    # ========================================================

    def send_command(self, command):

        if self.serial_port is None:
            self.log_message(
                f"Нет соединения: {command}"
            )
            return False

        try:

            self.serial_port.write(
                command.encode("ascii")
            )

            self.serial_port.flush()

            self.log_message(
                f">>> {command}"
            )

            return True

        except Exception as e:

            self.log_message(
                f"Ошибка TX: {e}"
            )

            return False

    # ========================================================
    # DEVICE ID
    # ========================================================

    def request_device_id(self):

        self.send_command(
            HELLO_COMMAND
        )

    def process_device_id(self, line):

        prefix = "DEVICE_ID_"

        suffix = "_YYY"

        if not line.startswith(prefix):
            return False

        if not line.endswith(suffix):
            return False

        device_id = line[
            len(prefix):-len(suffix)
        ]

        if len(device_id) != 24:
            return False

        try:
            int(device_id, 16)
        except ValueError:
            return False

        self.device_id = device_id

        self.device_state.setText(
            f"DEVICE: {device_id}"
        )

        self.log_message(
            f"DEVICE ID: {device_id}"
        )

        self.load_device_calibration()

        return True

    # ========================================================
    # CALIBRATION INI
    # ========================================================

    def load_device_calibration(self):

        if not INI_PATH.exists():

            self.clear_calibration()

            self.ini_label.setText(
                "INI: НЕТ ФАЙЛА"
            )

            return

        config = configparser.ConfigParser()

        try:
            config.read(
                INI_PATH,
                encoding="utf-8"
            )

            if "CALIBRATION" not in config:

                raise ValueError(
                    "Нет секции CALIBRATION"
                )

            section = config["CALIBRATION"]

            saved_device_id = section.get(
                "device_id",
                ""
            ).strip()

            zero_value = int(
                section.get(
                    "zero_value",
                    ""
                )
            )

            gain = float(
                section.get(
                    "gain_g_per_count",
                    ""
                )
            )

            if saved_device_id != self.device_id:
                raise ValueError(
                    "UID не совпадает"
                )

            if gain == 0.0:
                raise ValueError(
                    "GAIN = 0"
                )

            self.zero_raw = zero_value
            self.gain_g_per_count = gain

            self.update_calibration_labels()

            self.ini_label.setText(
                "INI: КАЛИБРОВКА ЗАГРУЖЕНА"
            )

            self.log_message(
                f"INI: ZERO={zero_value}, "
                f"GAIN={gain:.12g}"
            )

            self.send_cal_apply()

        except Exception as e:

            self.clear_calibration()

            self.ini_label.setText(
                "INI: НЕКОРРЕКТЕН"
            )

            self.log_message(
                f"INI не применён: {e}"
            )

            self.save_empty_ini()

    def save_ini(self):

        if not self.device_id:
            return

        if self.zero_raw is None:
            return

        if self.gain_g_per_count is None:
            return

        config = configparser.ConfigParser()

        config["CALIBRATION"] = {
            "device_id": self.device_id,
            "zero_value": str(
                self.zero_raw
            ),
            "gain_g_per_count": (
                f"{self.gain_g_per_count:.12g}"
            )
        }

        try:

            with open(
                INI_PATH,
                "w",
                encoding="utf-8"
            ) as file:

                config.write(file)

            self.ini_label.setText(
                "INI: СОХРАНЕНО"
            )

            self.log_message(
                f"INI сохранён: {INI_PATH}"
            )

        except Exception as e:

            self.log_message(
                f"Ошибка сохранения INI: {e}"
            )

    def save_empty_ini(self):

        try:

            if INI_PATH.exists():
                INI_PATH.unlink()

        except Exception:
            pass

    # ========================================================
    # CALIBRATION CLEAR
    # ========================================================

    def clear_calibration(self):

        self.zero_raw = None
        self.load_raw = None
        self.delta = None
        self.gain_g_per_count = None
        self.force_n = None

        self.calibration_applied = False
        self.calset_ok = False

        self.update_calibration_labels()

    # ========================================================
    # CALIBRATION APPLY
    # ========================================================

    def send_cal_apply(self):

        if self.zero_raw is None:
            return

        if self.gain_g_per_count is None:
            return

        command = (
            f"CALAPPLY_"
            f"{self.zero_raw}_"
            f"{self.gain_g_per_count:.12g}"
            f"_YYY"
        )

        self.send_command(command)

    # ========================================================
    # CAL ZERO
    # ========================================================

    def send_cal_zero(self):

        if self.serial_port is None:
            return

        self.send_command(
            CAL_ZERO_COMMAND
        )

        self.zero_raw = None
        self.load_raw = None
        self.delta = None
        self.gain_g_per_count = None

        self.update_calibration_labels()

        self.log_message(
            "Запущено измерение НУЛЯ."
        )

    # ========================================================
    # CAL LOAD
    # ========================================================

    def send_cal_load(self):

        if self.zero_raw is None:
            QMessageBox.warning(
                self,
                "Калибровка",
                "Сначала выполните калибровку нуля."
            )
            return

        self.send_command(
            CAL_LOAD_COMMAND
        )

        self.log_message(
            "Запущено измерение ГРУЗА."
        )

    # ========================================================
    # CAL GET
    # ========================================================

    def send_cal_get(self):

        self.send_command(
            CAL_GET_COMMAND
        )

    # ========================================================
    # CAL SET
    # ========================================================

    def send_cal_set(self):

        if self.zero_raw is None:
            QMessageBox.warning(
                self,
                "CALSET",
                "Нет ZERO_VALUE."
            )
            return

        if self.gain_g_per_count is None:
            QMessageBox.warning(
                self,
                "CALSET",
                "Сначала выполните CALGET / РАСЧЁТ."
            )
            return

        command = (
            f"CALSET_"
            f"{self.gain_g_per_count:.12g}"
            f"_YYY"
        )

        if self.send_command(command):

            self.calset_state_label.setText(
                "ОЖИДАНИЕ..."
            )

    # ========================================================
    # MASS
    # ========================================================

    def mass_changed(self, value):

        self.mass_g = float(value)

        self.mass_label.setText(
            f"{self.mass_g:.3f} g"
        )

        if self.delta is not None and self.delta != 0:

            self.gain_g_per_count = (
                self.mass_g / self.delta
            )

            self.force_n = (
                self.mass_g * GRAVITY
            )

            self.update_calibration_labels()

    # ========================================================
    # CALIBRATION LABELS
    # ========================================================

    def update_calibration_labels(self):

        self.zero_label.setText(
            "---"
            if self.zero_raw is None
            else str(self.zero_raw)
        )

        self.load_label.setText(
            "---"
            if self.load_raw is None
            else str(self.load_raw)
        )

        self.delta_label.setText(
            "---"
            if self.delta is None
            else str(self.delta)
        )

        self.mass_label.setText(
            f"{self.mass_g:.3f} g"
        )

        if self.force_n is None:
            self.force_label.setText(
                "---"
            )
        else:
            self.force_label.setText(
                f"{self.force_n:.6f} N"
            )

        if self.gain_g_per_count is None:
            self.gain_label.setText(
                "---"
            )
        else:
            self.gain_label.setText(
                f"{self.gain_g_per_count:.12g}"
            )

    # ========================================================
    # START MEASUREMENT
    # ========================================================

    def start_measurement(self):

        if self.serial_port is None:
            return

        if self.measurement_active:
            return

        if self.start_pending:
            return

        if self.stop_pending:
            return

        self.start_pending = True

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)

        self.measurement_status.setText(
            "START..."
        )

        self.measurement_status.setStyleSheet(
            f"color: {TEXT_DIM}; font-weight: bold;"
        )

        self.send_command(
            START_COMMAND
        )

    # ========================================================
    # STOP MEASUREMENT
    # ========================================================

    def stop_measurement(self):

        if self.serial_port is None:
            return

        if not self.measurement_active:
            return

        if self.stop_pending:
            return

        self.stop_pending = True

        self.stop_button.setEnabled(False)
        self.start_button.setEnabled(False)

        self.measurement_status.setText(
            "STOP..."
        )

        self.send_command(
            STOP_COMMAND
        )

    # ========================================================
    # START ACK
    # ========================================================

    def handle_start_ok(self):

        self.start_pending = False
        self.stop_pending = False

        self.measurement_active = True

        # Новое измерение начинается с нуля
        self.raw_data.clear()
        self.filtered_data.clear()
        self.force_data.clear()

        self.canvas.clear_plot()

        self.samples_label.setText(
            "SAMPLES: 0"
        )

        self.measurement_status.setText(
            "RUNNING"
        )

        self.measurement_status.setStyleSheet(
            f"color: {GREEN}; font-weight: bold;"
        )

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        # Калибровка во время измерения недоступна
        self.tabs.setTabEnabled(
            0,
            False
        )

        self.tabs.setCurrentIndex(1)

        self.log_message(
            "START подтверждён."
        )

    # ========================================================
    # STOP ACK
    # ========================================================

    def handle_stop_ok(self):

        self.start_pending = False
        self.stop_pending = False

        self.measurement_active = False

        self.measurement_status.setText(
            "STOPPED"
        )

        self.measurement_status.setStyleSheet(
            f"color: {RED}; font-weight: bold;"
        )

        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        # Калибровку снова можно использовать
        self.tabs.setTabEnabled(
            0,
            True
        )

        self.log_message(
            "STOP подтверждён."
        )

    # ========================================================
    # SERIAL PROCESS
    # ========================================================

    def process_serial(self):

        if self.serial_port is None:
            return

        try:

            waiting = self.serial_port.in_waiting

            if waiting <= 0:
                return

            data = self.serial_port.read(
                waiting
            )

            if not data:
                return

            self.rx_buffer.extend(data)

            self.parse_rx_buffer()

        except Exception as e:

            self.log_message(
                f"Ошибка RX: {e}"
            )

    # ========================================================
    # MIXED TEXT / BINARY PARSER
    # ========================================================

    def parse_rx_buffer(self):

        while self.rx_buffer:

            # ------------------------------------------------
            # Binary frame
            # ------------------------------------------------

            if self.rx_buffer[0] == 0xAA:

                if len(self.rx_buffer) < FRAME_SIZE:
                    return

                if self.rx_buffer[9] == 0xBB:

                    frame = bytes(
                        self.rx_buffer[:FRAME_SIZE]
                    )

                    del self.rx_buffer[
                        :FRAME_SIZE
                    ]

                    self.process_binary_frame(
                        frame
                    )

                    continue

                # Повреждённый бинарный кадр.
                # Сдвигаемся на один байт.
                del self.rx_buffer[0]

                continue

            # ------------------------------------------------
            # Text line
            # ------------------------------------------------

            newline_index = self.rx_buffer.find(
                b"\n"
            )

            aa_index = self.rx_buffer.find(
                b"\xAA"
            )

            # Если есть бинарный маркер раньше
            # найденного текста — удаляем мусор
            if (
                aa_index >= 0
                and (
                    newline_index < 0
                    or aa_index < newline_index
                )
            ):

                if aa_index > 0:
                    del self.rx_buffer[
                        :aa_index
                    ]

                    continue

                continue

            # Нет полной текстовой строки
            if newline_index < 0:
                return

            line_bytes = bytes(
                self.rx_buffer[
                    :newline_index + 1
                ]
            )

            del self.rx_buffer[
                :newline_index + 1
            ]

            try:

                line = line_bytes.decode(
                    "ascii",
                    errors="ignore"
                ).strip()

            except Exception:
                continue

            if line:
                self.process_text_line(
                    line
                )

    # ========================================================
    # BINARY FRAME
    # ========================================================

    def process_binary_frame(self, frame):

        try:
            marker_start, raw, filtered, force_n, marker_end = struct.unpack(
                FRAME_FORMAT,
                frame
            )
        except struct.error:
            return

        if marker_start != 0xAA:
            return

        if marker_end != 0xBB:
            return

        if not self.measurement_active:
            return

        self.raw_data.append(raw)
        self.filtered_data.append(filtered)
        self.force_data.append(force_n)

        if len(self.raw_data) > MAX_STORAGE:
            excess = len(self.raw_data) - MAX_STORAGE

            del self.raw_data[:excess]
            del self.filtered_data[:excess]
            del self.force_data[:excess]

        self.new_data_available = True

        self.samples_label.setText(
            f"SAMPLES: {len(self.raw_data)}"
        )

    # ========================================================
    # TEXT LINE
    # ========================================================

    def process_text_line(self, line):

        self.log_message(
            f"<<< {line}"
        )

        # ----------------------------------------------------
        # DEVICE ID
        # ----------------------------------------------------

        if self.process_device_id(line):
            return

        # ----------------------------------------------------
        # START
        # ----------------------------------------------------

        if line == "START_OK_YYY":

            self.handle_start_ok()
            return

        # ----------------------------------------------------
        # STOP
        # ----------------------------------------------------

        if line == "STOP_OK_YYY":

            self.handle_stop_ok()
            return

        # ----------------------------------------------------
        # CAL APPLY
        # ----------------------------------------------------

        if line == "CAL_APPLY_OK_YYY":

            self.calibration_applied = True

            self.log_message(
                "Калибровка применена STM32."
            )

            self.ini_label.setText(
                "INI: ПРИМЕНЕНО STM32"
            )

            return

        if line == "CAL_APPLY_ERROR_YYY":

            self.calibration_applied = False

            self.log_message(
                "STM32: ошибка CALAPPLY."
            )

            return

        # ----------------------------------------------------
        # CAL DATA
        # ----------------------------------------------------

        prefix = "CAL_DATA_"
        suffix = "_YYY"

        if (
            line.startswith(prefix)
            and line.endswith(suffix)
        ):

            body = line[
                len(prefix):-len(suffix)
            ]

            parts = body.split("_")

            if len(parts) == 2:

                try:

                    zero = int(parts[0])
                    load = int(parts[1])

                    self.handle_cal_data(
                        zero,
                        load
                    )

                except ValueError:
                    self.log_message(
                        "Ошибка CAL_DATA."
                    )

            return

        # ----------------------------------------------------
        # CAL NOT READY
        # ----------------------------------------------------

        if line == "CAL_NOT_READY_YYY":

            self.log_message(
                "STM32: CALIBRATION NOT READY."
            )

            QMessageBox.warning(
                self,
                "Калибровка",
                "STM32 не имеет готовых ZERO и LOAD."
            )

            return

        # ----------------------------------------------------
        # CAL SET
        # ----------------------------------------------------

        if line == "CAL_SET_OK_YYY":

            self.calset_ok = True

            self.calset_state_label.setText(
                "OK"
            )

            self.calset_state_label.setStyleSheet(
                f"""
                QLabel {{
                    color: {GREEN};
                    background: #0B0D0F;
                    border: 1px solid #292D32;
                    padding: 4px 8px;
                    font-family: Consolas;
                }}
                """
            )

            self.save_ini()

            self.log_message(
                "CALSET подтверждён STM32."
            )

            return

        if line == "CAL_SET_ERROR_YYY":

            self.calset_ok = False

            self.calset_state_label.setText(
                "ERROR"
            )

            self.calset_state_label.setStyleSheet(
                f"""
                QLabel {{
                    color: {RED};
                    background: #0B0D0F;
                    border: 1px solid #292D32;
                    padding: 4px 8px;
                    font-family: Consolas;
                }}
                """
            )

            self.log_message(
                "CALSET отклонён STM32."
            )

            return

    # ========================================================
    # CAL DATA
    # ========================================================

    def handle_cal_data(
        self,
        zero,
        load
    ):

        self.zero_raw = zero
        self.load_raw = load

        self.delta = (
            load - zero
        )

        self.mass_g = (
            self.mass_spin.value()
        )

        if self.delta == 0:

            self.gain_g_per_count = None
            self.force_n = None

            self.update_calibration_labels()

            QMessageBox.warning(
                self,
                "Калибровка",
                "ZERO и LOAD одинаковые."
            )

            return

        self.gain_g_per_count = (
            self.mass_g
            / self.delta
        )

        self.force_n = (
            self.mass_g
            * GRAVITY
        )

        self.calset_ok = False

        self.calset_state_label.setText(
            "НЕ ОТПРАВЛЕН"
        )

        self.update_calibration_labels()

        self.log_message(
            f"CALGET: ZERO={zero}, "
            f"LOAD={load}, "
            f"DELTA={self.delta}, "
            f"GAIN={self.gain_g_per_count:.12g}"
        )

    # ========================================================
    # PLOT UPDATE
    # ========================================================

    def update_measurement_plot(self):

        if not self.measurement_active:
            return

        if not self.raw_data:
            return

        self.canvas.update_plot(
            self.raw_data,
            self.filtered_data,
            self.force_data
        )

    # ========================================================
    # LOG
    # ========================================================

    def log_message(self, message):

        timestamp = time.strftime(
            "%H:%M:%S"
        )

        self.log.appendPlainText(
            f"[{timestamp}] {message}"
        )

        scrollbar = (
            self.log.verticalScrollBar()
        )

        scrollbar.setValue(
            scrollbar.maximum()
        )

    # ========================================================
    # CLOSE
    # ========================================================

    def closeEvent(self, event):

        if self.measurement_active:

            QMessageBox.warning(
                self,
                "Измерение",
                "Сначала остановите измерение."
            )

            event.ignore()
            return

        self.disconnect_device()

        event.accept()


# ============================================================
# MAIN
# ============================================================

def main():

    app = QApplication(sys.argv)

    app.setApplicationName(
        "STM32 ADS1220"
    )

    font = QFont(
        "Segoe UI",
        9
    )

    app.setFont(font)

    window = CalibrationWindow()
    window.show()

    sys.exit(
        app.exec_()
    )


if __name__ == "__main__":
    main()