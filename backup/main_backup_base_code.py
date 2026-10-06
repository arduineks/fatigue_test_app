import sys
import time
import struct
import configparser
from pathlib import Path

import serial
import serial.tools.list_ports

from PyQt5.QtCore import Qt, QTimer, QRectF
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
    QDoubleSpinBox,
    QTabWidget,
    QComboBox,
    QMessageBox,
    QFrame,
)


# ============================================================
# CONSTANTS
# ============================================================

SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.0

HELLO_COMMAND = "HELLO_YYY"

CAL_ZERO_COMMAND = "CALZERO_YYY"
CAL_LOAD_COMMAND = "CALLOAD_YYY"
CAL_GET_COMMAND = "CALGET_YYY"

CAL_APPLY_COMMAND = "CALAPPLY_YYY"

START_COMMAND = "START_YYY"
STOP_COMMAND = "STOP_YYY"

MEASUREMENT_FRAME_FORMAT = "<BiifB"
MEASUREMENT_FRAME_SIZE = struct.calcsize(
    MEASUREMENT_FRAME_FORMAT
)

FRAME_START = 0xAA
FRAME_END = 0xBB

GRAVITY = 0.00980665

INI_PATH = Path(__file__).resolve().with_name(
    "calibration.ini"
)


# ============================================================
# FORCE GRAPH
# ============================================================

class ForceGraphWidget(QWidget):

    def __init__(self, parent=None):

        super().__init__(parent)

        self.values = []
        self.running = False
        self.start_time = None

        self.setMinimumWidth(500)
        self.setMinimumHeight(300)

        # High contrast industrial dark theme
        self.background_color = QColor("#0B0F14")
        self.grid_color = QColor("#27313A")
        self.axis_color = QColor("#71808C")
        self.graph_color = QColor("#19E6FF")
        self.text_color = QColor("#F2F5F7")

    def start_measurement(self):

        self.values.clear()
        self.running = True
        self.start_time = time.time()

        self.update()

    def stop_measurement(self):

        self.running = False

        self.update()

    def clear(self):

        self.values.clear()
        self.running = False
        self.start_time = None

        self.update()

    def add_value(self, value):

        try:
            value = float(value)
        except (TypeError, ValueError):
            return

        self.values.append(value)

        if len(self.values) > 20000:
            self.values = self.values[-20000:]

        self.update()

    def paintEvent(self, event):

        painter = QPainter(self)
        painter.setRenderHint(
            QPainter.Antialiasing,
            True
        )

        rect = self.rect()

        painter.fillRect(
            rect,
            self.background_color
        )

        left = 70
        right = 25
        top = 25
        bottom = 45

        plot = QRectF(
            left,
            top,
            max(
                10,
                rect.width() - left - right
            ),
            max(
                10,
                rect.height() - top - bottom
            ),
        )

        # ----------------------------------------------------
        # Grid
        # ----------------------------------------------------

        painter.setPen(
            QPen(
                self.grid_color,
                1
            )
        )

        for i in range(1, 10):

            x = (
                plot.left()
                + plot.width() * i / 10.0
            )

            painter.drawLine(
                int(x),
                int(plot.top()),
                int(x),
                int(plot.bottom()),
            )

        for i in range(1, 5):

            y = (
                plot.top()
                + plot.height() * i / 5.0
            )

            painter.drawLine(
                int(plot.left()),
                int(y),
                int(plot.right()),
                int(y),
            )

        # ----------------------------------------------------
        # Axes
        # ----------------------------------------------------

        painter.setPen(
            QPen(
                self.axis_color,
                1
            )
        )

        painter.drawLine(
            int(plot.left()),
            int(plot.bottom()),
            int(plot.right()),
            int(plot.bottom()),
        )

        painter.drawLine(
            int(plot.left()),
            int(plot.top()),
            int(plot.left()),
            int(plot.bottom()),
        )

        # ----------------------------------------------------
        # Empty state
        # ----------------------------------------------------

        if not self.values:

            painter.setPen(
                QPen(self.text_color)
            )

            painter.setFont(
                QFont("Arial", 12)
            )

            painter.drawText(
                plot,
                Qt.AlignCenter,
                "ОЖИДАНИЕ ИЗМЕРЕНИЯ",
            )

            return

        # ----------------------------------------------------
        # Y range
        # ----------------------------------------------------

        min_value = min(self.values)
        max_value = max(self.values)

        if min_value == max_value:

            margin = max(
                abs(min_value) * 0.1,
                0.1
            )

            min_value -= margin
            max_value += margin

        else:

            margin = (
                max_value - min_value
            ) * 0.10

            min_value -= margin
            max_value += margin

        # ----------------------------------------------------
        # Y labels
        # ----------------------------------------------------

        painter.setPen(
            QPen(self.text_color)
        )

        painter.setFont(
            QFont("Arial", 9)
        )

        for i in range(6):

            ratio = i / 5.0

            value = (
                max_value
                - (max_value - min_value)
                * ratio
            )

            y = (
                plot.top()
                + plot.height() * ratio
            )

            painter.drawText(
                5,
                int(y - 8),
                left - 12,
                18,
                Qt.AlignRight
                | Qt.AlignVCenter,
                f"{value:.3f}",
            )

        # ----------------------------------------------------
        # X labels
        # ----------------------------------------------------

        if self.start_time is not None:

            elapsed = max(
                0.0,
                time.time()
                - self.start_time
            )

        else:

            elapsed = 0.0

        painter.drawText(
            int(plot.left()),
            int(plot.bottom() + 8),
            80,
            20,
            Qt.AlignLeft
            | Qt.AlignVCenter,
            "0 s",
        )

        painter.drawText(
            int(plot.right() - 80),
            int(plot.bottom() + 8),
            80,
            20,
            Qt.AlignRight
            | Qt.AlignVCenter,
            f"{elapsed:.1f} s",
        )

        # ----------------------------------------------------
        # Graph
        # ----------------------------------------------------

        if len(self.values) >= 2:

            painter.setPen(
                QPen(
                    self.graph_color,
                    2
                )
            )

            count = len(self.values)

            for i in range(1, count):

                x1 = (
                    plot.left()
                    + plot.width()
                    * (i - 1)
                    / (count - 1)
                )

                x2 = (
                    plot.left()
                    + plot.width()
                    * i
                    / (count - 1)
                )

                y1 = plot.bottom() - (
                    (
                        self.values[i - 1]
                        - min_value
                    )
                    / (
                        max_value
                        - min_value
                    )
                ) * plot.height()

                y2 = plot.bottom() - (
                    (
                        self.values[i]
                        - min_value
                    )
                    / (
                        max_value
                        - min_value
                    )
                ) * plot.height()

                painter.drawLine(
                    int(x1),
                    int(y1),
                    int(x2),
                    int(y2),
                )

        elif len(self.values) == 1:

            painter.setPen(
                QPen(
                    self.graph_color,
                    5
                )
            )

            x = plot.center().x()

            y = plot.bottom() - (
                (
                    self.values[0]
                    - min_value
                )
                / (
                    max_value
                    - min_value
                )
            ) * plot.height()

            painter.drawPoint(
                int(x),
                int(y)
            )


# ============================================================
# MAIN WINDOW
# ============================================================

class CalibrationWindow(QMainWindow):

    def __init__(self):

        super().__init__()

        self.setWindowTitle(
            "Fatigue Test — ADS1220"
        )

        self.resize(1200, 760)

        # ----------------------------------------------------
        # Serial
        # ----------------------------------------------------

        self.serial = None
        self.connected = False

        self.rx_buffer = bytearray()

        # ----------------------------------------------------
        # Device / calibration
        # ----------------------------------------------------

        self.device_id = None

        self.zero_value = None
        self.load_value = None

        self.gain_g_per_count = None

        self.calibration_mass_g = 0.0

        # ----------------------------------------------------
        # Measurement
        # ----------------------------------------------------

        self.measurement_running = False

        self.last_raw = 0
        self.last_filtered = 0
        self.last_force_n = 0.0

        self.frame_count = 0
        self.measurement_start_time = None

        # ----------------------------------------------------
        # UI
        # ----------------------------------------------------

        self.create_ui()

        # ----------------------------------------------------
        # Serial polling
        # ----------------------------------------------------

        self.serial_timer = QTimer(self)
        self.serial_timer.timeout.connect(
            self.poll_serial
        )
        self.serial_timer.start(5)

        # ----------------------------------------------------
        # Measurement timer
        # ----------------------------------------------------

        self.measurement_timer = QTimer(self)
        self.measurement_timer.timeout.connect(
            self.update_measurement_info
        )
        self.measurement_timer.start(100)

        self.refresh_ports()

    # ========================================================
    # UI STYLE
    # ========================================================

    def setup_styles(self):

        self.setStyleSheet("""
            QMainWindow {
                background: #0B0F14;
                color: #F2F5F7;
            }

            QWidget {
                background: #0B0F14;
                color: #F2F5F7;
                font-family: Arial;
                font-size: 10pt;
            }

            QGroupBox {
                border: 1px solid #33404A;
                border-radius: 5px;
                margin-top: 9px;
                padding: 10px 8px 8px 8px;
                font-weight: bold;
                color: #DCE5EA;
            }

            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #19E6FF;
            }

            QLabel {
                color: #E9EEF1;
            }

            QComboBox,
            QDoubleSpinBox {
                background: #121920;
                border: 1px solid #46535D;
                border-radius: 4px;
                padding: 5px 7px;
                color: #FFFFFF;
                min-height: 24px;
            }

            QComboBox:focus,
            QDoubleSpinBox:focus {
                border: 1px solid #19E6FF;
            }

            QPushButton {
                background: #17212A;
                border: 1px solid #52616C;
                border-radius: 4px;
                padding: 6px 14px;
                color: #FFFFFF;
                font-weight: bold;
                min-height: 25px;
            }

            QPushButton:hover {
                background: #21303B;
                border: 1px solid #19E6FF;
            }

            QPushButton:pressed {
                background: #0E171D;
            }

            QPushButton:disabled {
                background: #11161A;
                border: 1px solid #2A3238;
                color: #59636A;
            }

            QTextEdit {
                background: #080C10;
                border: 1px solid #33404A;
                border-radius: 4px;
                color: #DCE5EA;
                selection-background-color: #155A69;
                font-family: Consolas;
                font-size: 9pt;
            }

            QTabWidget::pane {
                border: 1px solid #33404A;
                background: #0B0F14;
            }

            QTabBar::tab {
                background: #121920;
                color: #9EAAB2;
                border: 1px solid #33404A;
                padding: 8px 18px;
                margin-right: 2px;
            }

            QTabBar::tab:selected {
                background: #1A2932;
                color: #19E6FF;
                border-bottom: 2px solid #19E6FF;
            }

            QTabBar::tab:hover {
                color: #FFFFFF;
            }
        """)

    # ========================================================
    # CREATE UI
    # ========================================================

    def create_ui(self):

        self.setup_styles()

        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(
            central
        )

        main_layout.setContentsMargins(
            8, 8, 8, 8
        )

        main_layout.setSpacing(7)

        # ----------------------------------------------------
        # Connection
        # ----------------------------------------------------

        connection_group = QGroupBox(
            "ПОДКЛЮЧЕНИЕ"
        )

        connection_layout = QHBoxLayout(
            connection_group
        )

        connection_layout.setContentsMargins(
            8, 6, 8, 6
        )

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(130)

        self.refresh_button = QPushButton(
            "Обновить"
        )

        self.refresh_button.clicked.connect(
            self.refresh_ports
        )

        self.connect_button = QPushButton(
            "Подключить"
        )

        self.connect_button.clicked.connect(
            self.toggle_connection
        )

        self.connection_status = QLabel(
            "Не подключено"
        )

        self.connection_status.setStyleSheet(
            "color: #FFB84D; font-weight: bold;"
        )

        connection_layout.addWidget(
            QLabel("Порт:")
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

        connection_layout.addSpacing(12)

        connection_layout.addWidget(
            self.connection_status
        )

        connection_layout.addStretch()

        main_layout.addWidget(
            connection_group
        )

        # ----------------------------------------------------
        # Tabs
        # ----------------------------------------------------

        self.tabs = QTabWidget()

        self.measurement_tab = (
            self.create_measurement_tab()
        )

        self.calibration_tab = (
            self.create_calibration_tab()
        )

        self.log_tab = (
            self.create_log_tab()
        )

        # REQUIRED ORDER:
        # Измерение - Калибровка - ЛОГ

        self.tabs.addTab(
            self.measurement_tab,
            "Измерение"
        )

        self.tabs.addTab(
            self.calibration_tab,
            "Калибровка датчика силы"
        )

        self.tabs.addTab(
            self.log_tab,
            "ЛОГ"
        )

        main_layout.addWidget(
            self.tabs,
            1
        )

    # ========================================================
    # CALIBRATION TAB
    # ========================================================

    def create_calibration_tab(self):

        tab = QWidget()

        layout = QHBoxLayout(tab)
        layout.setContentsMargins(
            5, 5, 5, 5
        )
        layout.setSpacing(8)

        # ====================================================
        # LEFT
        # ====================================================

        left = QVBoxLayout()
        left.setSpacing(7)

        # ----------------------------------------------------
        # Reference mass
        # ----------------------------------------------------

        mass_group = QGroupBox(
            "ЭТАЛОННЫЙ ГРУЗ"
        )

        mass_layout = QHBoxLayout(
            mass_group
        )

        mass_layout.setContentsMargins(
            8, 8, 8, 8
        )

        mass_layout.addWidget(
            QLabel("Масса:")
        )

        self.mass_edit = QDoubleSpinBox()

        self.mass_edit.setRange(
            0.001,
            1000000.0
        )

        self.mass_edit.setDecimals(3)
        self.mass_edit.setValue(500.0)

        self.mass_edit.setMinimumWidth(
            150
        )

        mass_layout.addWidget(
            self.mass_edit
        )

        mass_layout.addWidget(
            QLabel("г")
        )

        mass_layout.addStretch()

        left.addWidget(
            mass_group
        )

        # ----------------------------------------------------
        # Procedure
        # ----------------------------------------------------

        procedure_group = QGroupBox(
            "ПОСЛЕДОВАТЕЛЬНОСТЬ КАЛИБРОВКИ"
        )

        procedure_layout = QGridLayout(
            procedure_group
        )

        procedure_layout.setContentsMargins(
            8, 8, 8, 8
        )

        procedure_layout.setHorizontalSpacing(
            8
        )

        procedure_layout.setVerticalSpacing(
            7
        )

        # ZERO

        self.cal_zero_button = QPushButton(
            "1. ИЗМЕРИТЬ НОЛЬ"
        )

        self.cal_zero_button.clicked.connect(
            self.send_cal_zero
        )

        procedure_layout.addWidget(
            self.cal_zero_button,
            0,
            0
        )

        self.zero_value_label = QLabel(
            "—"
        )

        self.zero_value_label.setAlignment(
            Qt.AlignRight
            | Qt.AlignVCenter
        )

        procedure_layout.addWidget(
            self.zero_value_label,
            0,
            1
        )

        # LOAD

        self.cal_load_button = QPushButton(
            "2. ИЗМЕРИТЬ НАГРУЗКУ"
        )

        self.cal_load_button.clicked.connect(
            self.send_cal_load
        )

        procedure_layout.addWidget(
            self.cal_load_button,
            1,
            0
        )

        self.load_value_label = QLabel(
            "—"
        )

        self.load_value_label.setAlignment(
            Qt.AlignRight
            | Qt.AlignVCenter
        )

        procedure_layout.addWidget(
            self.load_value_label,
            1,
            1
        )

        # GET

        self.cal_get_button = QPushButton(
            "3. ПОЛУЧИТЬ ДАННЫЕ"
        )

        self.cal_get_button.clicked.connect(
            self.send_cal_get
        )

        procedure_layout.addWidget(
            self.cal_get_button,
            2,
            0
        )

        self.delta_value_label = QLabel(
            "—"
        )

        self.delta_value_label.setAlignment(
            Qt.AlignRight
            | Qt.AlignVCenter
        )

        procedure_layout.addWidget(
            self.delta_value_label,
            2,
            1
        )

        # GAIN

        gain_caption = QLabel(
            "GAIN:"
        )

        procedure_layout.addWidget(
            gain_caption,
            3,
            0
        )

        self.gain_value_label = QLabel(
            "—"
        )

        self.gain_value_label.setAlignment(
            Qt.AlignRight
            | Qt.AlignVCenter
        )

        procedure_layout.addWidget(
            self.gain_value_label,
            3,
            1
        )

        # SET GAIN

        self.cal_set_button = QPushButton(
            "4. УСТАНОВИТЬ GAIN"
        )

        self.cal_set_button.clicked.connect(
            self.send_cal_set
        )

        procedure_layout.addWidget(
            self.cal_set_button,
            4,
            0,
            1,
            2
        )

        left.addWidget(
            procedure_group
        )

        # ----------------------------------------------------
        # Current calibration
        # ----------------------------------------------------

        result_group = QGroupBox(
            "ТЕКУЩАЯ КАЛИБРОВКА"
        )

        result_layout = QGridLayout(
            result_group
        )

        result_layout.setContentsMargins(
            8, 8, 8, 8
        )

        result_layout.addWidget(
            QLabel("DEVICE ID:"),
            0,
            0
        )

        self.device_id_label = QLabel(
            "—"
        )

        self.device_id_label.setStyleSheet(
            "color: #19E6FF; "
            "font-weight: bold;"
        )

        result_layout.addWidget(
            self.device_id_label,
            0,
            1
        )

        result_layout.addWidget(
            QLabel("ZERO:"),
            1,
            0
        )

        self.current_zero_label = QLabel(
            "—"
        )

        result_layout.addWidget(
            self.current_zero_label,
            1,
            1
        )

        result_layout.addWidget(
            QLabel("GAIN:"),
            2,
            0
        )

        self.current_gain_label = QLabel(
            "—"
        )

        result_layout.addWidget(
            self.current_gain_label,
            2,
            1
        )

        left.addWidget(
            result_group
        )

        # ----------------------------------------------------
        # DEVICE ID RX
        # ----------------------------------------------------

        device_rx_group = QGroupBox(
            "ПРИНЯТЫЙ DEVICE ID ОТ STM32"
        )

        device_rx_layout = QVBoxLayout(
            device_rx_group
        )

        device_rx_layout.setContentsMargins(
            8, 8, 8, 8
        )

        self.device_rx_label = QLabel(
            "Ожидание DEVICE_ID..."
        )

        self.device_rx_label.setWordWrap(
            True
        )

        self.device_rx_label.setStyleSheet(
            "background: #080C10; "
            "border: 1px solid #33404A; "
            "border-radius: 4px; "
            "padding: 7px; "
            "color: #7CFFB2; "
            "font-family: Consolas; "
            "font-weight: bold;"
        )

        device_rx_layout.addWidget(
            self.device_rx_label
        )

        left.addWidget(
            device_rx_group
        )

        left.addStretch()

        # ====================================================
        # RIGHT — CALIBRATION LOG
        # ====================================================

        calibration_log_group = QGroupBox(
            "ЛОГ КАЛИБРОВКИ"
        )

        calibration_log_layout = QVBoxLayout(
            calibration_log_group
        )

        calibration_log_layout.setContentsMargins(
            7, 7, 7, 7
        )

        self.calibration_log = QTextEdit()
        self.calibration_log.setReadOnly(
            True
        )

        calibration_log_layout.addWidget(
            self.calibration_log
        )

        layout.addLayout(
            left,
            0
        )

        layout.addWidget(
            calibration_log_group,
            1
        )

        return tab

    # ========================================================
    # GLOBAL LOG TAB
    # ========================================================

    def create_log_tab(self):

        tab = QWidget()

        layout = QVBoxLayout(tab)

        layout.setContentsMargins(
            5, 5, 5, 5
        )

        group = QGroupBox(
            "ОБЩИЙ СИСТЕМНЫЙ ЛОГ"
        )

        group_layout = QVBoxLayout(
            group
        )

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        group_layout.addWidget(
            self.log
        )

        layout.addWidget(
            group
        )

        return tab

    # ========================================================
    # MEASUREMENT TAB
    # ========================================================

    def create_measurement_tab(self):

        tab = QWidget()

        layout = QHBoxLayout(tab)

        layout.setContentsMargins(
            5, 5, 5, 5
        )

        layout.setSpacing(8)

        # ====================================================
        # LEFT PANEL
        # ====================================================

        left = QVBoxLayout()

        left.setSpacing(7)

        # ----------------------------------------------------
        # Control
        # ----------------------------------------------------

        control_group = QGroupBox(
            "УПРАВЛЕНИЕ"
        )

        control_layout = QHBoxLayout(
            control_group
        )

        control_layout.setContentsMargins(
            7, 7, 7, 7
        )

        self.start_button = QPushButton(
            "START"
        )

        self.start_button.setMinimumWidth(
            90
        )

        self.start_button.clicked.connect(
            self.send_start
        )

        self.stop_button = QPushButton(
            "STOP"
        )

        self.stop_button.setMinimumWidth(
            90
        )

        self.stop_button.clicked.connect(
            self.send_stop
        )

        self.start_button.setEnabled(
            False
        )

        self.stop_button.setEnabled(
            False
        )

        control_layout.addWidget(
            self.start_button
        )

        control_layout.addWidget(
            self.stop_button
        )

        left.addWidget(
            control_group
        )

        # ----------------------------------------------------
        # Force indicator
        # ----------------------------------------------------

        force_group = QGroupBox(
            "УСИЛИЕ"
        )

        force_layout = QVBoxLayout(
            force_group
        )

        force_layout.setContentsMargins(
            10, 10, 10, 10
        )

        force_title = QLabel(
            "ТЕКУЩЕЕ ЗНАЧЕНИЕ"
        )

        force_title.setStyleSheet(
            "color: #8B9AA5;"
        )

        force_layout.addWidget(
            force_title
        )

        self.measurement_force_label = QLabel(
            "0.000000 N"
        )

        self.measurement_force_label.setAlignment(
            Qt.AlignCenter
        )

        self.measurement_force_label.setMinimumHeight(
            55
        )

        self.measurement_force_label.setStyleSheet(
            "background: #08151A; "
            "border: 1px solid #1C7F90; "
            "border-radius: 5px; "
            "color: #19E6FF; "
            "font-size: 23pt; "
            "font-weight: bold; "
            "padding: 4px;"
        )

        force_layout.addWidget(
            self.measurement_force_label
        )

        left.addWidget(
            force_group
        )

        # ----------------------------------------------------
        # Counters
        # ----------------------------------------------------

        status_group = QGroupBox(
            "СОСТОЯНИЕ"
        )

        status_layout = QGridLayout(
            status_group
        )

        status_layout.setContentsMargins(
            8, 8, 8, 8
        )

        status_layout.addWidget(
            QLabel("КАДРЫ"),
            0,
            0
        )

        self.measurement_frame_count_label = QLabel(
            "0"
        )

        self.measurement_frame_count_label.setAlignment(
            Qt.AlignRight
        )

        status_layout.addWidget(
            self.measurement_frame_count_label,
            0,
            1
        )

        status_layout.addWidget(
            QLabel("ВРЕМЯ"),
            1,
            0
        )

        self.measurement_time_label = QLabel(
            "0.000 s"
        )

        self.measurement_time_label.setAlignment(
            Qt.AlignRight
        )

        status_layout.addWidget(
            self.measurement_time_label,
            1,
            1
        )

        left.addWidget(
            status_group
        )

        left.addStretch()

        # ====================================================
        # RIGHT — GRAPH
        # ====================================================

        graph_group = QGroupBox(
            "УСИЛИЕ — FORCE_N"
        )

        graph_layout = QVBoxLayout(
            graph_group
        )

        graph_layout.setContentsMargins(
            5, 5, 5, 5
        )

        self.force_graph = ForceGraphWidget()

        graph_layout.addWidget(
            self.force_graph
        )

        layout.addLayout(
            left,
            0
        )

        layout.addWidget(
            graph_group,
            1
        )

        return tab

    # ========================================================
    # TIME
    # ========================================================

    def timestamp(self):

        return (
            time.strftime("%H:%M:%S")
            + f"{int((time.time() % 1) * 1000):03d}"
        )

    # ========================================================
    # GLOBAL LOG
    # ========================================================

    def append_log(self, text):

        timestamp = self.timestamp()

        self.log.append(
            f"[{timestamp}] {text}"
        )

        self.log.ensureCursorVisible()

    # ========================================================
    # CALIBRATION LOG
    # ========================================================

    def append_calibration_log(self, text):

        timestamp = self.timestamp()

        self.calibration_log.append(
            f"[{timestamp}] {text}"
        )

        self.calibration_log.ensureCursorVisible()

    # ========================================================
    # PORTS
    # ========================================================

    def refresh_ports(self):

        current = self.port_combo.currentText()

        self.port_combo.clear()

        ports = serial.tools.list_ports.comports()

        for port in ports:

            self.port_combo.addItem(
                port.device
            )

        if current:

            index = self.port_combo.findText(
                current
            )

            if index >= 0:

                self.port_combo.setCurrentIndex(
                    index
                )

    # ========================================================
    # CONNECTION
    # ========================================================

    def toggle_connection(self):

        if self.connected:

            self.disconnect_serial()

        else:

            self.connect_serial()

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

            self.start_button.setEnabled(
                True
            )

            self.stop_button.setEnabled(
                False
            )

            self.send_command(
                HELLO_COMMAND
            )

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

        if self.serial is not None:

            try:
                self.serial.close()
            except Exception:
                pass

        self.serial = None
        self.connected = False

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
    # SEND COMMAND
    # ========================================================

    def send_command(self, command):

        if (
            not self.connected
            or self.serial is None
        ):

            self.append_log(
                f"TX ERROR — нет подключения: "
                f"{command}"
            )

            return False

        try:

            data = command.encode(
                "ascii"
            )

            self.serial.write(data)
            self.serial.flush()

            self.append_log(
                f">>> TX: {command}"
            )

            return True

        except Exception as e:

            self.append_log(
                f"TX ERROR: {e}"
            )

            return False

    # ========================================================
    # CALIBRATION COMMANDS
    # ========================================================

    def send_cal_zero(self):

        self.append_calibration_log(
            f">>> TX: {CAL_ZERO_COMMAND}"
        )

        self.send_command(
            CAL_ZERO_COMMAND
        )

    def send_cal_load(self):

        mass = self.mass_edit.value()

        if mass <= 0:

            QMessageBox.warning(
                self,
                "Калибровка",
                "Введите массу эталонного груза."
            )

            return

        self.append_calibration_log(
            f"Эталонный груз: "
            f"{mass:.3f} г"
        )

        self.append_calibration_log(
            f">>> TX: {CAL_LOAD_COMMAND}"
        )

        self.send_command(
            CAL_LOAD_COMMAND
        )

    def send_cal_get(self):

        self.append_calibration_log(
            f">>> TX: {CAL_GET_COMMAND}"
        )

        self.send_command(
            CAL_GET_COMMAND
        )

    def send_cal_set(self):

        if self.gain_g_per_count is None:

            self.append_calibration_log(
                "CALSET ERROR: "
                "GAIN отсутствует"
            )

            QMessageBox.warning(
                self,
                "Калибровка",
                "Сначала необходимо получить "
                "данные калибровки."
            )

            return

        command = (
            f"CALSET_"
            f"{self.gain_g_per_count:.12f}"
            f"_YYY"
        )

        self.append_calibration_log(
            f">>> TX: {command}"
        )

        self.send_command(
            command
        )

    # ========================================================
    # SERIAL POLLING
    # ========================================================

    def poll_serial(self):

        if (
            not self.connected
            or self.serial is None
        ):
            return

        try:

            waiting = self.serial.in_waiting

            if waiting <= 0:
                return

            data = self.serial.read(
                min(waiting, 4096)
            )

            if not data:
                return

            self.rx_buffer.extend(data)

            self.parse_rx()

        except Exception as e:

            self.append_log(
                f"RX ERROR: {e}"
            )

    # ========================================================
    # RX PARSER
    # ========================================================

    def parse_rx(self):

        while self.rx_buffer:

            # ------------------------------------------------
            # Binary frame
            # ------------------------------------------------

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

            # ------------------------------------------------
            # Text frame
            # ------------------------------------------------

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

            # ------------------------------------------------
            # Text before binary frame
            # ------------------------------------------------

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

        # Полный RX остается в общем логе.
        self.append_log(
            f"<<< RX: {text} "
            f"[HEX: {hex_data}]"
        )

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

        self.frame_count += 1

        hex_data = frame.hex(
            " "
        ).upper()

        self.append_log(
            f"<<< RX [{hex_data}] | "
            f"RAW={raw} | "
            f"FILTERED={filtered} | "
            f"FORCE_N={force_n:.6f} N"
        )

        # ----------------------------------------------------
        # Only force is displayed in measurement UI.
        # RAW and FILTERED remain in the global log.
        # ----------------------------------------------------

        self.measurement_force_label.setText(
            f"{force_n:.3f} N"
        )

        self.measurement_frame_count_label.setText(
            str(self.frame_count)
        )

        # ----------------------------------------------------
        # FORCE_N comes directly from STM32.
        # No recalculation.
        # ----------------------------------------------------

        if self.measurement_running:

            self.force_graph.add_value(
                force_n
            )

    # ========================================================
    # STM32 RESPONSES
    # ========================================================

    def process_response(self, line):

        # ----------------------------------------------------
        # START
        # ----------------------------------------------------

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

            return

        # ----------------------------------------------------
        # STOP
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # DEVICE ID
        # ----------------------------------------------------

        if line.startswith(
            "DEVICE_ID_"
        ):

            self.process_device_id(
                line
            )

            return

        # ----------------------------------------------------
        # CAL ZERO
        # ----------------------------------------------------

        if line == "CAL_ZERO_OK_YYY":

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        # ----------------------------------------------------
        # CAL LOAD
        # ----------------------------------------------------

        if line == "CAL_LOAD_OK_YYY":

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        # ----------------------------------------------------
        # CAL NOT READY
        # ----------------------------------------------------

        if line == "CAL_NOT_READY_YYY":

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        # ----------------------------------------------------
        # CAL DATA
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # CAL SET OK
        # ----------------------------------------------------

        if line.startswith(
            "CAL_SET_OK_"
        ):

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        # ----------------------------------------------------
        # CAL SET ERROR
        # ----------------------------------------------------

        if line.startswith(
            "CAL_SET_ERROR_"
        ):

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        # ----------------------------------------------------
        # CAL APPLY OK
        # ----------------------------------------------------

        if line.startswith(
            "CAL_APPLY_OK_"
        ):

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        # ----------------------------------------------------
        # CAL APPLY ERROR
        # ----------------------------------------------------

        if line.startswith(
            "CAL_APPLY_ERROR_"
        ):

            self.append_calibration_log(
                f"<<< RX: {line}"
            )

            return

        # ----------------------------------------------------
        # Other STM32 text
        # ----------------------------------------------------

        self.append_log(
            f"STM32: {line}"
        )

    # ========================================================
    # DEVICE ID
    # ========================================================

    def process_device_id(self, line):

        # Expected:
        # DEVICE_ID_<UID>_YYY

        try:

            parts = line.split("_")

            if len(parts) < 3:
                return

            device_id = parts[1]

            self.device_id = device_id

            # ------------------------------------------------
            # Main calibration display
            # ------------------------------------------------

            self.device_id_label.setText(
                device_id
            )

            # ------------------------------------------------
            # Explicit STM32 RX display
            # ------------------------------------------------

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

    def process_cal_data(self, line):

        # Expected:
        # CAL_DATA_<ZERO>_<LOAD>_YYY

        try:

            parts = line.split("_")

            if len(parts) < 5:
                return

            zero_raw = int(parts[2])
            load_raw = int(parts[3])

            self.zero_value = zero_raw
            self.load_value = load_raw

            self.zero_value_label.setText(
                str(zero_raw)
            )

            self.load_value_label.setText(
                str(load_raw)
            )

            self.current_zero_label.setText(
                str(zero_raw)
            )

            delta = load_raw - zero_raw

            self.delta_value_label.setText(
                str(delta)
            )

            self.append_calibration_log(
                f"ZERO RAW = {zero_raw}"
            )

            self.append_calibration_log(
                f"LOAD RAW = {load_raw}"
            )

            self.append_calibration_log(
                f"DELTA = {delta}"
            )

            mass_g = self.mass_edit.value()

            self.calibration_mass_g = mass_g

            if delta == 0:

                self.append_calibration_log(
                    "ERROR: DELTA = 0"
                )

                self.gain_g_per_count = None

                return

            # ------------------------------------------------
            # EXISTING CALIBRATION FORMULA
            # ------------------------------------------------

            self.gain_g_per_count = (
                mass_g / delta
            )

            force_n = (
                mass_g * GRAVITY
            )

            # ------------------------------------------------
            # UI
            # ------------------------------------------------

            self.gain_value_label.setText(
                f"{self.gain_g_per_count:.12f}"
            )

            self.current_gain_label.setText(
                f"{self.gain_g_per_count:.12f}"
            )

            self.append_calibration_log(
                f"MASS = {mass_g:.3f} g"
            )

            self.append_calibration_log(
                f"FORCE = {force_n:.6f} N"
            )

            self.append_calibration_log(
                "GAIN = "
                f"{self.gain_g_per_count:.12f} "
                f"g/count"
            )

            # ------------------------------------------------
            # Save calibration
            # ------------------------------------------------

            self.save_calibration()

        except Exception as e:

            self.append_calibration_log(
                f"CAL_DATA ERROR: {e}"
            )

    # ========================================================
    # CALIBRATION INI
    # ========================================================

    def load_calibration(self):

        if not self.device_id:
            return

        config = configparser.ConfigParser()

        if not INI_PATH.exists():

            self.append_calibration_log(
                f"Calibration INI not found: "
                f"{INI_PATH}"
            )

            return

        try:

            config.read(
                INI_PATH,
                encoding="utf-8"
            )

            if not config.has_section(
                self.device_id
            ):

                self.append_calibration_log(
                    f"No calibration for "
                    f"DEVICE_ID="
                    f"{self.device_id}"
                )

                return

            section = config[
                self.device_id
            ]

            zero_text = section.get(
                "zero_value",
                fallback=""
            )

            gain_text = section.get(
                "gain_g_per_count",
                fallback=""
            )

            if zero_text:

                self.zero_value = int(
                    zero_text
                )

            if gain_text:

                self.gain_g_per_count = float(
                    gain_text
                )

            if self.zero_value is not None:

                self.current_zero_label.setText(
                    str(self.zero_value)
                )

            if (
                self.gain_g_per_count
                is not None
            ):

                self.current_gain_label.setText(
                    f"{self.gain_g_per_count:.12f}"
                )

            self.append_calibration_log(
                f"Calibration loaded for "
                f"DEVICE_ID="
                f"{self.device_id}"
            )

            # ------------------------------------------------
            # Automatically apply saved calibration.
            #
            # CALAPPLY button is intentionally absent
            # from the UI.
            # ------------------------------------------------

            if (
                self.zero_value is not None
                and self.gain_g_per_count
                is not None
            ):

                command = (
                    f"CALAPPLY_"
                    f"{self.zero_value}_"
                    f"{self.gain_g_per_count:.12f}_"
                    f"YYY"
                )

                self.append_calibration_log(
                    f">>> TX: {command}"
                )

                self.send_command(
                    command
                )

        except Exception as e:

            self.append_calibration_log(
                f"Calibration load ERROR: {e}"
            )

    def save_calibration(self):

        if not self.device_id:
            return

        if (
            self.zero_value is None
            or self.gain_g_per_count is None
        ):
            return

        config = configparser.ConfigParser()

        if INI_PATH.exists():

            config.read(
                INI_PATH,
                encoding="utf-8"
            )

        if not config.has_section(
            self.device_id
        ):

            config.add_section(
                self.device_id
            )

        config[
            self.device_id
        ]["zero_value"] = str(
            self.zero_value
        )

        config[
            self.device_id
        ]["gain_g_per_count"] = (
            f"{self.gain_g_per_count:.12f}"
        )

        try:

            with open(
                INI_PATH,
                "w",
                encoding="utf-8"
            ) as f:

                config.write(f)

            self.append_calibration_log(
                f"Calibration saved: "
                f"{INI_PATH}"
            )

        except Exception as e:

            self.append_calibration_log(
                f"Calibration save ERROR: "
                f"{e}"
            )

    # ========================================================
    # START
    # ========================================================

    def send_start(self):

        if (
            not self.connected
            or self.serial is None
        ):
            return

        self.frame_count = 0

        self.last_raw = 0
        self.last_filtered = 0
        self.last_force_n = 0.0

        self.measurement_start_time = None

        self.measurement_force_label.setText(
            "0.000000 N"
        )

        self.measurement_frame_count_label.setText(
            "0"
        )

        self.measurement_time_label.setText(
            "0.000 s"
        )

        self.force_graph.clear()

        self.send_command(
            START_COMMAND
        )

    # ========================================================
    # STOP
    # ========================================================

    def send_stop(self):

        if (
            not self.connected
            or self.serial is None
        ):
            return

        self.send_command(
            STOP_COMMAND
        )

    # ========================================================
    # MEASUREMENT INFO
    # ========================================================

    def update_measurement_info(self):

        if (
            self.measurement_running
            and self.measurement_start_time
            is not None
        ):

            elapsed = (
                time.time()
                - self.measurement_start_time
            )

            self.measurement_time_label.setText(
                f"{elapsed:.3f} s"
            )


# ============================================================
# APPLICATION
# ============================================================

def main():

    app = QApplication(sys.argv)

    app.setStyle(
        "Fusion"
    )

    window = CalibrationWindow()

    window.show()

    sys.exit(
        app.exec_()
    )


if __name__ == "__main__":
    main()