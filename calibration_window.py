import sys
import time
import struct
import configparser
from pathlib import Path

import serial
import serial.tools.list_ports

from PyQt5.QtCore import Qt, QTimer
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
    QMenu,
    QLineEdit,
    QCheckBox,
)

from protocol import (
    SERIAL_BAUD,
    SERIAL_TIMEOUT,
    HELLO_COMMAND,
    CAL_ZERO_COMMAND,
    CAL_LOAD_COMMAND,
    CAL_GET_COMMAND,
    CAL_APPLY_COMMAND,
    START_COMMAND,
    STOP_COMMAND,
    MEASUREMENT_FRAME_FORMAT,
    MEASUREMENT_FRAME_SIZE,
    FRAME_START,
    FRAME_END,
    GRAVITY,
    INI_PATH,
)
from graph_widget import ForceGraphWidget

class CalibrationWindow(QMainWindow):

    def __init__(self):

        super().__init__()

        self.setWindowTitle(
            "Fatigue Test"
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
        self.last_current_mm = 0.0

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

        # ----------------------------------------------------
        # Force maintaining regulator
        # ----------------------------------------------------

        self.maintain_active = False
        self.maintain_last_direction = 0
        self.maintain_wait_until = 0.0
        self.maintain_caught = False
        # Скользящее окно (время, сила) для принятия решения.
        self.maintain_window = []

        self.maintain_timer = QTimer(self)
        self.maintain_timer.timeout.connect(
            self.maintain_force_step
        )

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

        self.tabs.addTab(
            self.measurement_tab,
            "Измерение"
        )

        self.tabs.addTab(
            QWidget(),
            "Данные испытания"
        )

        self.tabs.addTab(
            QWidget(),
            "Управление траверсой"
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

    def set_force_value_color(self, color):

        self.measurement_force_label.setStyleSheet(
            "background: #08151A; "
            "border: 1px solid #1C7F90; "
            "border-radius: 5px; "
            f"color: {color.name()}; "
            "font-size: 23pt; "
            "font-weight: bold; "
            "padding: 4px;"
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

        left = QVBoxLayout()
        left.setSpacing(7)

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
            "0.000 N"
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

        # =====================================================
        # ПОЛОЖЕНИЕ ТРАВЕРСЫ
        # =====================================================

        traverse_group = QGroupBox(
            "ПОЛОЖЕНИЕ ТРАВЕРСЫ"
        )

        traverse_layout = QVBoxLayout(
            traverse_group
        )

        traverse_layout.setContentsMargins(
            8,
            8,
            8,
            8,
        )

        traverse_layout.setSpacing(4)

        self.traverse_position_label = QLabel(
            "0.000 mm"
        )

        self.traverse_target_edit = QLineEdit()
        self.traverse_target_edit.setPlaceholderText("Точка, мм")
        self.traverse_target_edit.setText("0.000")

        self.traverse_speed_edit = QLineEdit()
        self.traverse_speed_edit.setPlaceholderText("Скорость, мм/с")
        self.traverse_speed_edit.setText("0.500")

        self.traverse_move_button = QPushButton("ПЕРЕМЕСТИТЬ")
        self.traverse_move_button.clicked.connect(
            self.move_traverse_to_target
        )

        self.traverse_position_label.setAlignment(
            Qt.AlignCenter
        )

        self.traverse_position_label.setMinimumHeight(
            55
        )

        self.traverse_position_label.setStyleSheet(
            "background: #08151A; "
            "border: 1px solid #1C7F90; "
            "border-radius: 5px; "
            "color: #19E6FF; "
            "font-size: 23pt; "
            "font-weight: bold; "
            "padding: 4px;"
        )

        traverse_layout.addWidget(
            self.traverse_position_label
        )

        traverse_layout.addWidget(
            self.traverse_target_edit
        )

        traverse_layout.addWidget(
            self.traverse_speed_edit
        )

        traverse_layout.addWidget(
            self.traverse_move_button
        )

        left.addWidget(
            traverse_group
        )

        # =====================================================
        # ПОДДЕРЖАНИЕ ЗАДАННОЙ СИЛЫ
        # =====================================================
        # Регулятор: поднимает/опускает траверсу командами
        # MOVE, пока сила не достигнет целевой, затем
        # удерживает её в пределах допуска. Подразумевается,
        # что подъём траверсы (увеличение мм) увеличивает силу.

        maintain_group = QGroupBox(
            "ПОДДЕРЖАНИЕ СИЛЫ"
        )

        maintain_layout = QVBoxLayout(
            maintain_group
        )

        maintain_layout.setContentsMargins(
            8,
            8,
            8,
            8,
        )

        maintain_layout.setSpacing(4)

        self.maintain_force_edit = QLineEdit()
        self.maintain_force_edit.setPlaceholderText(
            "Таргетная сила, Н"
        )
        self.maintain_force_edit.setText("1.000")

        self.maintain_speed_edit = QLineEdit()
        self.maintain_speed_edit.setPlaceholderText(
            "Скорость, мм/с"
        )
        self.maintain_speed_edit.setText("0.500")

        self.maintain_auto_speed_check = QCheckBox(
            "Автоматическое вычисление скорости"
        )

        self.maintain_auto_speed_check.setToolTip(
            "Скорость каждого перемещения вычисляется "
            "пропорционально ошибке по силе "
            "(поле скорости игнорируется)"
        )

        self.maintain_button = QPushButton(
            "НАЧАТЬ ПОДДЕРЖИВАТЬ"
        )
        self.maintain_button.clicked.connect(
            self.toggle_maintain_force
        )

        maintain_layout.addWidget(
            self.maintain_force_edit
        )

        maintain_layout.addWidget(
            self.maintain_speed_edit
        )

        maintain_layout.addWidget(
            self.maintain_auto_speed_check
        )

        maintain_layout.addWidget(
            self.maintain_button
        )

        left.addWidget(
            maintain_group
        )

        # =====================================================
        # ПОЛОЖЕНИЕ ТРАВЕРСЫ
        # =====================================================

        # =====================================================
        # АНАЛИЗ ЦИКЛА
        # =====================================================

        cycle_count_group = QGroupBox(
            "АНАЛИЗ ЦИКЛА"
        )

        cycle_count_layout = QHBoxLayout(
            cycle_count_group
        )

        cycle_count_layout.setContentsMargins(
            8,
            8,
            8,
            8,
        )

        cycle_count_layout.setSpacing(6)

        # -----------------------------------------------------
        # Функция создания карточки
        # -----------------------------------------------------

        def create_cycle_card(
                title,
                value,
                color,
        ):
            card = QFrame()
            card.setStyleSheet(
                "QFrame {"
                "background: #08151A;"
                "border: 1px solid #26343C;"
                "border-radius: 6px;"
                "}"
            )

            card_layout = QVBoxLayout(card)

            card_layout.setContentsMargins(
                6,
                5,
                6,
                5,
            )

            card_layout.setSpacing(2)

            title_label = QLabel(
                title
            )

            title_label.setAlignment(
                Qt.AlignCenter
            )

            title_label.setStyleSheet(
                f"color: {color}; "
                "font-size: 9pt; "
                "font-weight: bold; "
                "border: none; "
                "background: transparent;"
            )

            value_label = QLabel(
                value
            )

            value_label.setAlignment(
                Qt.AlignCenter
            )

            value_label.setMinimumHeight(
                32
            )

            value_label.setStyleSheet(
                "color: #F2F5F7; "
                "font-size: 15pt; "
                "font-weight: bold; "
                "border: none; "
                "background: transparent;"
            )

            card_layout.addWidget(
                title_label
            )

            card_layout.addWidget(
                value_label
            )

            cycle_count_layout.addWidget(
                card
            )

            return value_label

        # -----------------------------------------------------
        # Карточки анализа цикла
        # -----------------------------------------------------

        self.cycle_min_force_label = create_cycle_card(
            "МИН СИЛА",
            "0.00 N",
            "#19E6FF",
        )

        self.cycle_max_force_label = create_cycle_card(
            "МАКС СИЛА",
            "0.00 N",
            "#FF6B6B",
        )

        self.cycle_average_force_label = create_cycle_card(
            "СРЕДНЕЕ",
            "0.00 N",
            "#FFD400",
        )

        self.cycle_amplitude_force_label = create_cycle_card(
            "АМПЛИТУДА",
            "0.00 N",
            "#C77DFF",
        )

        self.cycle_count_label = create_cycle_card(
            "КОЛИЧЕСТВО ЦИКЛОВ",
            "0",
            "#6BEF83",
        )

        left.addWidget(
            cycle_count_group
        )

        # =====================================================
        # СТАТУС ДАННЫХ
        # =====================================================

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

        graph_layout.setSpacing(0)

        # ----------------------------------------------------
        # GRAPH
        # ----------------------------------------------------

        self.force_graph = ForceGraphWidget()

        self.force_graph.on_force_color_changed = (
            self.set_force_value_color
        )

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
    # GRAPH CONTROL HELPERS
    # ========================================================

    def create_line_style_combo(self):

        combo = QComboBox()

        combo.setMinimumWidth(
            75
        )

        combo.addItem(
            "Сплошная",
            Qt.SolidLine
        )

        combo.addItem(
            "Штриховая",
            Qt.DashLine
        )

        combo.addItem(
            "Точечная",
            Qt.DotLine
        )

        combo.addItem(
            "Штрих-точка",
            Qt.DashDotLine
        )

        return combo

    def set_color_button(
            self,
            button,
            color
    ):

        button.setStyleSheet(
            "QPushButton {"
            f"background: {color.name()};"
            "border: 1px solid #71808C;"
            "border-radius: 3px;"
            "padding: 0px;"
            "min-height: 20px;"
            "}"
            "QPushButton:hover {"
            "border: 1px solid #FFFFFF;"
            "}"
        )

    def choose_curve_color(
            self,
            curve_name
    ):

        if curve_name == "FORCE_N":

            current_color = (
                self.force_graph.force_color
            )

        elif curve_name == "RAW":

            current_color = (
                self.force_graph.raw_color
            )

        else:

            current_color = (
                self.force_graph.filtered_color
            )

        color = QColorDialog.getColor(
            current_color,
            self,
            f"Цвет линии — {curve_name}"
        )

        if not color.isValid():
            return

        if curve_name == "FORCE_N":

            self.force_graph.set_force_color(
                color
            )

            self.set_color_button(
                self.force_color_button,
                color
            )

        elif curve_name == "RAW":

            self.force_graph.set_raw_color(
                color
            )

            self.set_color_button(
                self.raw_color_button,
                color
            )

        else:

            self.force_graph.set_filtered_color(
                color
            )

            self.set_color_button(
                self.filtered_color_button,
                color
            )

    def change_curve_style(
            self,
            curve_name,
            index
    ):

        combo = {
            "FORCE_N": self.force_style_combo,
            "RAW": self.raw_style_combo,
            "FILTERED": self.filtered_style_combo,
        }[curve_name]

        style = combo.itemData(
            index
        )

        if style is None:
            return

        if curve_name == "FORCE_N":

            self.force_graph.set_force_style(
                style
            )

        elif curve_name == "RAW":

            self.force_graph.set_raw_style(
                style
            )

        else:

            self.force_graph.set_filtered_style(
                style
            )

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

        self.frame_count += 1

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

        self.measurement_force_label.setText(
            f"{force_n:.3f} N"
        )

        self.traverse_position_label.setText(
            f"{current_mm:.3f} mm"
        )

        self.measurement_frame_count_label.setText(
            str(self.frame_count)
        )

        # ----------------------------------------------------
        # FORCE_N, RAW and FILTERED come directly from STM32.
        # FORCE_N is NOT recalculated here.
        # ----------------------------------------------------

        if self.measurement_running:
            self.force_graph.add_frame(
                raw,
                filtered,
                force_n
            )

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

    def process_cal_data(self, line):

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

            self.gain_g_per_count = (
                    mass_g / delta
            )

            force_n = (
                    mass_g * GRAVITY
            )

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
        self.last_current_mm = 0.0

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

        # ----------------------------------------------------
        # Показатели, вычисляемые из входных данных.
        # Обновляются из последних принятых значений и
        # состояния анализа циклов в ForceGraphWidget.
        # ----------------------------------------------------

        if self.last_force_n is not None:
            self.measurement_force_label.setText(
                f"{self.last_force_n:.3f} N"
            )

        if self.last_current_mm is not None:
            self.traverse_position_label.setText(
                f"{self.last_current_mm:.3f} mm"
            )

        self.measurement_frame_count_label.setText(
            str(self.frame_count)
        )

        graph = self.force_graph

        # Карточки соответствуют линиям MIN/MAX на графике
        # (экстремумы последнего завершённого цикла),
        # а не текущим экстремумам «в моменте».

        if graph.cycle_min is not None:
            self.cycle_min_force_label.setText(
                f"{graph.cycle_min:.2f} N"
            )

        if graph.cycle_max is not None:
            self.cycle_max_force_label.setText(
                f"{graph.cycle_max:.2f} N"
            )

        if (
                graph.cycle_min is not None
                and graph.cycle_max is not None
        ):
            mean_force = (
                    graph.cycle_min
                    + graph.cycle_max
            ) / 2.0

            amplitude_force = (
                    graph.cycle_max
                    - graph.cycle_min
            ) / 2.0

            self.cycle_average_force_label.setText(
                f"{mean_force:.2f} N"
            )

            self.cycle_amplitude_force_label.setText(
                f"{amplitude_force:.2f} N"
            )

        self.cycle_count_label.setText(
            str(graph.cycle_count)
        )

    # ========================================================
    # DISP TRAVERS
    # ========================================================
    def move_traverse_to_target(self):
        try:
            target_mm = float(
                self.traverse_target_edit.text().replace(",", ".")
            )
            speed_mm_s = float(
                self.traverse_speed_edit.text().replace(",", ".")
            )
        except ValueError:
            self.append_log(
                "ОШИБКА: некорректная точка или скорость"
            )
            return

        if speed_mm_s <= 0:
            self.append_log(
                "ОШИБКА: скорость должна быть больше 0"
            )
            return

        command = (
            f"MOVE_0_{target_mm:.3f}_{speed_mm_s:.3f}_YYY"
        )

        self.send_command(command)

    # ========================================================
    # FORCE MAINTAINING
    # ========================================================
    # Регулятор удержания заданной силы: пока сила ниже цели
    # (сверх допуска) — поднимает траверсу малыми шагами,
    # выше цели — опускает, в допуске — не двигает.
    # Подъём = увеличение координаты (мм).

    MAINTAIN_FORCE_TOLERANCE = 0.05   # Н, полоса допуска
    MAINTAIN_STEP_MM = 0.2            # мм, макс. шаг за такт
    MAINTAIN_PERIOD_MS = 200          # такт регулятора
    MAINTAIN_SETTLE_S = 0.3           # запас, с, сверх времени хода
    # Окно усреднения силы для принятия решения, с.
    MAINTAIN_WINDOW_S = 4.0
    # Авто-скорость: мм/с на 1 Н ошибки, с ограничениями.
    MAINTAIN_AUTO_SPEED_GAIN = 1.0    # (мм/с)/Н
    MAINTAIN_AUTO_SPEED_MIN = 0.05    # мм/с
    MAINTAIN_AUTO_SPEED_MAX = 2.0     # мм/с
    # Подъём траверсы увеличивает силу; если на стенде наоборот —
    # поставить False (направление регулятора инвертируется).
    MAINTAIN_UP_INCREASES_FORCE = True

    def toggle_maintain_force(self):

        if (
                not self.connected
                or self.serial is None
        ):
            self.append_log(
                "ОШИБКА: нет подключения к устройству"
            )
            return

        if not self.maintain_active:

            try:
                target_n = float(
                    self.maintain_force_edit.text().replace(
                        ",", "."
                    )
                )
                speed_mm_s = float(
                    self.maintain_speed_edit.text().replace(
                        ",", "."
                    )
                )
            except ValueError:
                self.append_log(
                    "ОШИБКА: некорректная сила или скорость"
                )
                return

            if target_n < 0:
                self.append_log(
                    "ОШИБКА: сила должна быть >= 0"
                )
                return

            if speed_mm_s <= 0:
                self.append_log(
                    "ОШИБКА: скорость должна быть > 0"
                )
                return

            self.maintain_target_n = target_n
            self.maintain_speed_mm_s = speed_mm_s
            self.maintain_active = True
            self.maintain_last_direction = 0
            self.maintain_caught = False
            self.maintain_window = []
            self.maintain_wait_until = 0.0

            self.maintain_button.setText(
                "ОСТАНОВИТЬ ПОДДЕРЖИВАНИЕ"
            )
            self.maintain_timer.start(
                self.MAINTAIN_PERIOD_MS
            )

            self.append_log(
                f"MAINTAIN: старт, цель {target_n:.3f} N, "
                f"скорость {speed_mm_s:.3f} mm/s"
            )

        else:

            self.maintain_active = False
            self.maintain_caught = False
            self.maintain_window = []
            self.maintain_timer.stop()

            self.maintain_button.setText(
                "НАЧАТЬ ПОДДЕРЖИВАТЬ"
            )

            # Остановка движения: команда в текущую позицию.
            if self.last_current_mm is not None:
                self.send_command(
                    f"MOVE_0_{self.last_current_mm:.3f}_"
                    f"{self.maintain_speed_mm_s:.3f}_YYY"
                )

            self.append_log(
                "MAINTAIN: остановлено"
            )

    def maintain_force_step(self):

        if not self.maintain_active:
            return

        now = time.time()

        force = self.last_force_n
        current_mm = self.last_current_mm

        if (
                force is None
                or current_mm is None
        ):
            return

        # ----------------------------------------------------
        # Скользящее окно силы: решение принимается по
        # среднему за окно, а не по мгновенному значению.
        # ----------------------------------------------------

        self.maintain_window.append(
            (now, force)
        )

        window_start = now - self.MAINTAIN_WINDOW_S

        while (
                self.maintain_window
                and self.maintain_window[0][0] < window_start
        ):
            del self.maintain_window[0]

        if not self.maintain_window:
            return

        window_mean = (
                sum(f for _, f in self.maintain_window)
                / len(self.maintain_window)
        )

        error = (
                self.maintain_target_n
                - window_mean
        )

        if not self.MAINTAIN_UP_INCREASES_FORCE:
            error = -error

        tolerance = self.MAINTAIN_FORCE_TOLERANCE

        # ----------------------------------------------------
        # Цель поймана: прекратить подстройки. Возобновить —
        # только когда среднее за окно выйдет из допуска.
        # ----------------------------------------------------

        if self.maintain_caught:

            if abs(error) <= tolerance:
                return

            self.maintain_caught = False
            self.maintain_last_direction = 0
            self.append_log(
                f"MAINTAIN: выход из допуска "
                f"(ср. {window_mean:.3f} N), "
                f"подстройки возобновлены"
            )

        else:

            if abs(error) <= tolerance:

                self.maintain_caught = True
                self.maintain_last_direction = 0

                # Остановка движения: команда в текущую позицию.
                self.send_command(
                    f"MOVE_0_{current_mm:.3f}_"
                    f"{self.maintain_speed_mm_s:.3f}_YYY"
                )

                self.append_log(
                    f"MAINTAIN: цель поймана "
                    f"(ср. {window_mean:.3f} N), "
                    f"подстройки остановлены"
                )

                return

        # ----------------------------------------------------
        # Не слать следующий MOVE, пока не завершится
        # предыдущий: иначе команды копятся в очереди
        # MCU и траверса дёргается верх-низ.
        # ----------------------------------------------------

        if now < self.maintain_wait_until:
            return

        # ----------------------------------------------------
        # Скорость: из поля или автоматически —
        # пропорционально ошибке по силе.
        # ----------------------------------------------------

        if self.maintain_auto_speed_check.isChecked():
            speed_mm_s = (
                    self.MAINTAIN_AUTO_SPEED_GAIN
                    * abs(
                        self.maintain_target_n
                        - window_mean
                    )
            )

            speed_mm_s = max(
                self.MAINTAIN_AUTO_SPEED_MIN,
                min(
                    self.MAINTAIN_AUTO_SPEED_MAX,
                    speed_mm_s,
                ),
            )
        else:
            speed_mm_s = self.maintain_speed_mm_s

        direction = 1 if error > 0 else -1

        # Шаг пропорционален ошибке: далеко от цели —
        # максимальный шаг, вблизи — мельче.
        step_mm = (
                self.MAINTAIN_STEP_MM
                * abs(error) / 1.0
        )

        step_mm = max(
            0.02,
            min(self.MAINTAIN_STEP_MM, step_mm),
        )

        target_mm = (
                current_mm
                + direction * step_mm
        )

        self.send_command(
            f"MOVE_0_{target_mm:.3f}_"
            f"{speed_mm_s:.3f}_YYY"
        )

        # Пауза до конца хода + запас на реакцию датчика.
        self.maintain_wait_until = (
                now
                + step_mm / speed_mm_s
                + self.MAINTAIN_SETTLE_S
        )

        # В лог — только смена направления движения,
        # иначе лог захлебнётся.
        if direction != self.maintain_last_direction:
            self.maintain_last_direction = direction
            self.append_log(
                f"MAINTAIN: ср. {window_mean:.3f} N, "
                f"движение {'вверх' if direction > 0 else 'вниз'} "
                f"(шаг {step_mm:.3f} мм, {speed_mm_s:.3f} мм/с)"
            )


# ============================================================
# APPLICATION
# ============================================================
