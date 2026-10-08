"""MainWindow — главное окно приложения (бывший
CalibrationWindow из calibration_window.py). Монолит разобран
на модули-миксины: traverse_safety, traverse_regulator,
serial_protocol, calibration, measurement_session,
app_settings_io; виджеты — widgets.py.
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
from src.session_recorder import (
    SessionRecorder,
    parse_interval,
    format_interval_hint,
)
from src.logging_setup import logger


from src.widgets import ToggleSwitch, CollapsibleGroupBox
from src.traverse_safety import TraverseSafetyMixin
from src.traverse_regulator import TraverseRegulatorMixin
from src.serial_protocol import SerialProtocolMixin
from src.calibration import CalibrationMixin
from src.measurement_session import MeasurementSessionMixin
from src.app_settings_io import AppSettingsIOMixin


class MainWindow(
        QMainWindow,
        TraverseSafetyMixin,
        TraverseRegulatorMixin,
        SerialProtocolMixin,
        CalibrationMixin,
        MeasurementSessionMixin,
        AppSettingsIOMixin,
):


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
        self.cycle_times = []
        self.last_cycle_count = 0
        self.cpm_history = []
        self.live_min_force = None
        self.live_max_force = None
        self.speed_samples = []
        self.measured_speed_mm_s = None
        self.measurement_start_time = None

        # ----------------------------------------------------
        # Калибровка траверсы по усилию (диалог «Калибровка»)
        # ----------------------------------------------------
        # Параметры команды HOMEFORCE, сохраняются в секции
        # [HOME] app_settings.ini; читаются/пишутся диалогом.
        self.home_speed_text = "0.1"
        self.home_detect_text = "0.1"
        self.home_retract_text = "5"

        # ----------------------------------------------------
        # Cycle target (цель качества цикла: σ_max, R → σ_min)
        # ----------------------------------------------------
        # Канонические значения в Н; поля ввода — в текущих
        # единицах отображения (Н/МПа).
        self.cycle_target_sigma_max_n = None
        self.cycle_target_sigma_min_n = None
        self._cycle_target_mpa_warned = False

        # ----------------------------------------------------
        # UI
        # ----------------------------------------------------

        # Запись сессии испытания (этап 0).
        self.session_recorder = SessionRecorder(self)

        # Поставщик кадров данных графика: периодическая запись
        # кадров (frames.csv) с частотой интервала записи —
        # независимо от завершения циклов.
        self.session_recorder.set_frame_provider(
            lambda seconds: self.force_graph.get_recent_frames(
                seconds, absolute=True,
            )
        )

        self.create_ui()

        # Настройки приложения (имя образца, интервал, путь) —
        # отдельный app_settings.ini; загружаются после создания
        # полей вкладки «Настройки».
        self.load_app_settings()

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
        # Аварийное окно открыто (защита от наслоения).
        self._emergency_dialog_open = False
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

            QGroupBox#collapsible::title {
                left: 34px;
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

            QLineEdit {
                background: #121920;
                border: 1px solid #71808C;
                border-radius: 4px;
                padding: 5px 7px;
                color: #FFFFFF;
                min-height: 24px;
            }

            QLineEdit:focus {
                border: 1px solid #19E6FF;
            }

            QLineEdit:disabled {
                color: #8B9AA5;
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

        # ----------------------------------------------------
        # Интерактивный режим: команды применяются сразу
        # при изменении значений в полях ввода, без кнопок.
        # ----------------------------------------------------

        self.interactive_mode_check = ToggleSwitch(
            "Интерактивный режим"
        )

        self.interactive_mode_check.setChecked(
            True
        )

        self.interactive_mode_check.setToolTip(
            "Изменение значений в полях ввода применяется "
            "сразу (Enter или потеря фокуса), без нажатия "
            "кнопок подтверждения"
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

        self.data_tab = (
            self.create_data_tab()
        )

        self.settings_tab = (
            self.create_settings_tab()
        )

        self.log_tab = (
            self.create_log_tab()
        )

        self.tabs.addTab(
            self.measurement_tab,
            "Измерение"
        )

        self.tabs.addTab(
            self.data_tab,
            "Данные испытания"
        )

        self.tabs.addTab(
            self.settings_tab,
            "Настройки"
        )

        self.tabs.addTab(
            self.calibration_tab,
            "Калибровка датчика силы"
        )

        self.tabs.addTab(
            self.log_tab,
            "ЛОГ"
        )

        # ----------------------------------------------------
        # Режим ввода: интерактивный тумблер — отдельный
        # блок верхней строки.
        # ----------------------------------------------------

        interactive_group = QGroupBox(
            "РЕЖИМ ВВОДА"
        )

        interactive_layout = QVBoxLayout(
            interactive_group
        )

        interactive_layout.addWidget(
            self.interactive_mode_check
        )

        # ----------------------------------------------------
        # Сброс сессии — отдельный блок верхней строки,
        # слева от «РЕЖИМ ВВОДА».
        # ----------------------------------------------------

        reset_group = QGroupBox(
            "СБРОС"
        )

        reset_layout = QVBoxLayout(
            reset_group
        )

        self.reset_button = QPushButton(
            "СБРОС"
        )

        self.reset_button.setMinimumWidth(
            90
        )

        self.reset_button.clicked.connect(
            self.reset_session
        )

        reset_layout.addWidget(
            self.reset_button
        )

        # ----------------------------------------------------
        # Запись данных — отдельный блок верхней строки.
        # Кнопка-тумблер: старт записи создаёт НОВУЮ папку
        # сессии всегда (даже если запись уже активна).
        # ----------------------------------------------------

        record_group = QGroupBox(
            "ЗАПИСЬ"
        )

        record_layout = QVBoxLayout(
            record_group
        )

        self.record_button = QPushButton(
            "СТАРТ ЗАПИСИ"
        )

        self.record_button.setMinimumWidth(
            110
        )

        self.record_button.clicked.connect(
            self.toggle_recording
        )

        record_layout.addWidget(
            self.record_button
        )

        # ----------------------------------------------------
        top_row = QHBoxLayout()
        top_row.setSpacing(7)

        top_row.addWidget(
            connection_group,
            1
        )

        top_row.addWidget(
            reset_group,
            0
        )

        top_row.addWidget(
            record_group,
            0
        )

        top_row.addWidget(
            interactive_group,
            0
        )

        main_layout.addLayout(
            top_row
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

    def set_mean_value_color(self, color):

        # Цвет окна (карточки) «СРЕДНЕЕ» = цвет белой (MID)
        # линии; цвет текста не меняется.
        self.set_cycle_card_window_color(
            self.cycle_average_force_label,
            color,
        )

    def set_min_value_color(self, color):

        # Цвет окна «МИН СИЛА» = цвет MIN-линии графика.
        self.set_cycle_card_window_color(
            self.cycle_min_force_label,
            color,
        )

    def set_max_value_color(self, color):

        # Цвет окна «МАКС СИЛА» = цвет MAX-линии графика.
        self.set_cycle_card_window_color(
            self.cycle_max_force_label,
            color,
        )

    def set_cycle_card_window_color(self, value_label, color):

        c = QColor(color)

        card = getattr(value_label, "card_frame", None)

        if card is None:
            return

        card.setStyleSheet(
            "QFrame {"
            f"background: rgba({c.red()}, {c.green()}, {c.blue()}, 60);"
            "border: 1px solid #1C7F90;"
            "border-radius: 5px;"
            "}"
        )

    # ========================================================
    # SPECIMEN / UNITS
    # ========================================================

    def get_specimen_area_mm2(self):

        try:
            width_mm = float(
                self.specimen_width_edit.text().replace(
                    ",", "."
                )
            )
            thickness_mm = float(
                self.specimen_thickness_edit.text().replace(
                    ",", "."
                )
            )
        except ValueError:
            return None

        if (
                width_mm <= 0
                or thickness_mm <= 0
        ):
            return None

        return width_mm * thickness_mm

    def update_specimen_area(self):

        area = self.get_specimen_area_mm2()

        if area is not None:
            self.specimen_area_label.setText(
                f"Площадь: {area:.2f} мм²"
            )
        else:
            self.specimen_area_label.setText(
                "Площадь: — мм²"
            )

        # Во время измерения площадь меняется на лету:
        # масштаб Y строится от максимума записи, при смене
        # площади значения «скачут» — масштаб надо пересчитать
        # с нуля под новую единицу, иначе кривая уходит за края.
        self.force_graph.reset_y_scale()
        self.force_graph.set_area_mm2(area)

        # Пересчитать карточки в текущих единицах.
        self.update_measurement_info()

        # Целевые линии цикла зависят от площади (МПа → Н).
        self._update_cycle_targets()

    def convert_force_value(self, force_n):
        # Н → единицы отображения (если выбраны МПа
        # и известна площадь сечения).

        if self.force_display_units == "MPa":
            area = self.get_specimen_area_mm2()

            if area is not None:
                return (
                        force_n
                        / area
                )

        return force_n

    def convert_display_to_n(self, value):
        # Обратная конвертация: значение в выбранных
        # единицах (поле целевой силы) → Н для регулятора.

        if self.maintain_units == "MPa":
            area = self.get_specimen_area_mm2()

            if area is None:
                return None

            return (
                    value
                    * area
            )

        return value

    def current_force_suffix(self):
        # Суффикс единиц для карточек/полей силы.

        if (
                self.force_display_units == "MPa"
                and self.get_specimen_area_mm2()
                is not None
        ):
            return "MPa"

        return "N"

    def set_force_display_units(self, units):
        # Режим единиц силы (Н/МПа) — кнопками вместо
        # выпадающего списка. МПа без площади образца
        # не применяется (суффикс остаётся N).
        if units not in ("N", "MPa"):
            return

        self.force_display_units = units

        self.force_graph.set_display_units(units)

        self._update_force_units_buttons()

        # Кнопки единиц цели поддержания следуют за режимом
        # вывода, значение цели пересчитывается.
        self._sync_maintain_units_to_display()

        # Целевые параметры цикла — в новых единицах (внутри Н).
        self._sync_cycle_target_units_to_display()
        self._update_cycle_targets()

        # Пересчитать карточки.
        self.update_measurement_info()

    def _sync_cycle_target_units_to_display(self):
        # Переписать поле σ_max в новых единицах отображения;
        # каноническое значение (Н) не меняется.
        if self.cycle_target_sigma_max_n is None:
            return

        value_display = self.convert_force_value(
            self.cycle_target_sigma_max_n
        )

        self.cycle_target_sigma_max_edit.setText(
            f"{value_display:.3f}"
        )

    def _update_cycle_targets(self):
        # Целевые параметры качества цикла: σ_max (в текущих
        # единицах отображения) и R → σ_min = R·σ_max. Внутри
        # хранятся в Н (sigma_max_n / sigma_min_n). Невалидно
        # (σ_max ≤ 0, R вне [−1, 1]) → линии скрыты, σ_min «—».
        try:
            sigma_max_display = float(
                self.cycle_target_sigma_max_edit.text()
                .replace(",", ".")
            )
            r_value = float(
                self.cycle_target_r_edit.text()
                .replace(",", ".")
            )
        except ValueError:
            sigma_max_display = None
            r_value = None

        sigma_max_n = None
        sigma_min_n = None
        sigma_min_display = None

        if (
                sigma_max_display is not None
                and r_value is not None
                and sigma_max_display > 0
                and -1.0 <= r_value <= 1.0
        ):
            sigma_max_n = self.convert_display_to_n(
                sigma_max_display
            )

            if sigma_max_n is None:
                # МПа без площади образца — пересчёт невозможен.
                if not self._cycle_target_mpa_warned:
                    self.append_log(
                        "ЦЕЛЬ ЦИКЛА: для МПа задайте размеры образца"
                    )
                    self._cycle_target_mpa_warned = True

                sigma_max_n = None

            else:
                sigma_min_n = r_value * sigma_max_n
                sigma_min_display = r_value * sigma_max_display

        self.cycle_target_sigma_max_n = sigma_max_n
        self.cycle_target_sigma_min_n = sigma_min_n

        if sigma_min_display is not None:
            self.cycle_target_sigma_min_label.setText(
                f"{sigma_min_display:.2f} {self.current_force_suffix()}"
            )
        else:
            self.cycle_target_sigma_min_label.setText("—")

        self.force_graph.set_target_lines(
            sigma_max_n,
            sigma_min_n,
        )

        self.force_graph.set_target_lines_visible(
            self.cycle_target_lines_check.isChecked()
        )

    def _sync_maintain_units_to_display(self):
        # Единицы цели поддержания по умолчанию Н; при
        # переключении режима вывода переключаются вместе
        # с ним, значение цели пересчитывается (Н ↔ МПа по
        # площади образца). Кнопками единицы можно сменить
        # вручную в любой момент — при следующем переключении
        # режима синхронизация выполняется снова. Цель
        # регулятора хранится в Н и не меняется.
        new_units = self.force_display_units

        if new_units == self.maintain_units:
            return

        if "MPa" in (new_units, self.maintain_units):
            area = self.get_specimen_area_mm2()

            if area is None:
                # В МПа без площади не перейти.
                return

            try:
                value = float(
                    self.maintain_force_edit.text().replace(",", ".")
                )
            except ValueError:
                self.set_maintain_units(new_units)
                return

            force_n = (
                value * area
                if self.maintain_units == "MPa"
                else value
            )

            new_value = (
                force_n / area
                if new_units == "MPa"
                else force_n
            )

            self.maintain_force_edit.setText(
                f"{new_value:.3f}"
            )

        self.set_maintain_units(new_units)

    def _unit_buttons_style(self, units, active_units):
        # Активная кнопка — бирюзовая (в цвет интерфейса),
        # неактивная — серая.
        active_style = (
            "QPushButton {"
            "background: #1C7F90; "
            "border: 1px solid #19E6FF; "
            "border-radius: 4px; "
            "color: #FFFFFF; "
            "font-weight: bold; "
            "padding: 6px 10px;"
            "}"
        )

        inactive_style = (
            "QPushButton {"
            "background: #52616C; "
            "border: 1px solid #33404A; "
            "border-radius: 4px; "
            "color: #DCE5EA; "
            "font-weight: bold; "
            "padding: 6px 10px;"
            "}"
        )

        return (
            active_style
            if units == active_units
            else inactive_style
        )

    def _update_force_units_buttons(self):

        self.force_units_n_button.setStyleSheet(
            self._unit_buttons_style("N", self.force_display_units)
        )

        self.force_units_mpa_button.setStyleSheet(
            self._unit_buttons_style("MPa", self.force_display_units)
        )

    def set_maintain_units(self, units):
        # Единицы целевой силы поддержания: Н / МПа.
        if units not in ("N", "MPa"):
            return

        self.maintain_units = units

        self._update_maintain_units_buttons()

    def _update_maintain_units_buttons(self):

        self.maintain_units_n_button.setStyleSheet(
            self._unit_buttons_style("N", self.maintain_units)
        )

        self.maintain_units_mpa_button.setStyleSheet(
            self._unit_buttons_style("MPa", self.maintain_units)
        )

    # ========================================================
    # INTERACTIVE MODE
    # ========================================================
    # В интерактивном режиме изменение значения поля ввода
    # (Enter или потеря фокуса) сразу применяется по назначению
    # поля, без нажатия кнопок подтверждения.

    def on_interactive_field_changed(self):

        if (
                not self.connected
                or self.serial is None
        ):
            return

        sender = self.sender()

        if sender is self.traverse_target_edit:
            self.move_traverse_to_target()

        elif sender is self.traverse_speed_edit:
            # Скорость сама по себе команды не шлёт —
            # она применится при следующем перемещении.
            pass

        elif sender is self.maintain_force_edit:

            try:
                target_n = float(
                    self.maintain_force_edit.text().replace(
                        ",", "."
                    )
                )
            except ValueError:
                return

            if target_n >= 0:
                target_n_actual = self.convert_display_to_n(
                    target_n
                )

                if target_n_actual is None:
                    self.append_log(
                        "ОШИБКА: для МПа задайте размеры образца"
                    )
                    return

                self.maintain_target_n = target_n_actual

                self.append_log(
                    f"MAINTAIN: новая цель "
                    f"{target_n:.3f} {self.maintain_units}"
                    f" = {target_n_actual:.3f} N"
                )

        elif sender is self.maintain_speed_edit:

            if self.maintain_auto_speed_check.isChecked():
                return

            try:
                speed_mm_s = float(
                    self.maintain_speed_edit.text().replace(
                        ",", "."
                    )
                )
            except ValueError:
                return

            if speed_mm_s > 0:
                self.maintain_speed_mm_s = speed_mm_s

    # ========================================================
    # CYCLE TARGET → MAINTAIN (цель поддержания = SIG_M)
    # ========================================================

    def _cycle_sigma_m_n(self):
        # Середина целевого цикла (SIG_M) в Н; None если
        # целевые параметры невалидны/не заданы.
        if (
                self.cycle_target_sigma_max_n is None
                or self.cycle_target_sigma_min_n is None
        ):
            return None

        return (
                self.cycle_target_sigma_max_n
                + self.cycle_target_sigma_min_n
        ) / 2.0

    def _n_to_maintain_units(self, force_n):
        # Н → единицы цели поддержания (Н/МПа по площади).
        if force_n is None:
            return None

        if self.maintain_units == "MPa":
            area = self.get_specimen_area_mm2()

            if area is None:
                return None

            return force_n / area

        return force_n

    def apply_cycle_target_to_maintain(self):
        # Кнопка «σ_M → цель»: вносит σ_M (середину целевого
        # цикла) в поле цели поддержания в его единицах.
        # Регулятор НЕ ретаргетируется и не запускается —
        # оператор сам стартует поддержание, и только тогда
        # поле читается (toggle_maintain_force).
        sigma_m_n = self._cycle_sigma_m_n()

        if sigma_m_n is None:
            self.append_log(
                "MAINTAIN: целевые параметры не заданы"
            )
            return

        display_value = self._n_to_maintain_units(sigma_m_n)

        if display_value is None:
            self.append_log(
                "MAINTAIN: не задана площадь образца — "
                "σ_M не внесён"
            )
            return

        self.maintain_force_edit.setText(
            f"{display_value:.3f}"
        )

        self.append_log(
            f"MAINTAIN: в поле цели внесено σ_M = "
            f"{display_value:.3f} ({sigma_m_n:.3f} N)"
        )

    def on_maintain_auto_speed_toggled(self, checked):
        # При включённой авто-скорости поле скорости траверсы
        # неактивно. Во время поддержания блоком управляет
        # set_traverse_block_enabled — не перебиваем его.
        if not getattr(self, "maintain_active", False):
            self.traverse_speed_edit.setEnabled(not checked)

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

        # QPlainTextEdit + лимит блоков: append O(1), старые строки
        # вытесняются — вкладка не фризится на больших объёмах.
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)

        group_layout.addWidget(
            self.log
        )

        layout.addWidget(
            group
        )

        return tab

    # ========================================================
    # DATA TAB (экспорт сессии)
    # ========================================================

    def create_data_tab(self):

        tab = QWidget()

        layout = QVBoxLayout(tab)

        layout.setContentsMargins(
            5, 5, 5, 5
        )

        layout.setSpacing(8)

        group = QGroupBox(
            "ЭКСПОРТ ДАННЫХ ИСПЫТАНИЯ"
        )

        group_layout = QVBoxLayout(
            group
        )

        group_layout.setContentsMargins(
            10, 10, 10, 10
        )

        group_layout.setSpacing(8)

        self.export_pdf_button = QPushButton(
            "Экспорт PDF"
        )

        self.export_pdf_button.setMinimumWidth(
            140
        )

        self.export_pdf_button.clicked.connect(
            self.export_session_pdf
        )

        self.export_csv_button = QPushButton(
            "Экспорт CSV"
        )

        self.export_csv_button.setMinimumWidth(
            140
        )

        self.export_csv_button.clicked.connect(
            self.export_session_csv
        )

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        button_row.addWidget(
            self.export_pdf_button
        )

        button_row.addWidget(
            self.export_csv_button
        )

        button_row.addStretch()

        group_layout.addLayout(
            button_row
        )

        self.last_session_label = QLabel(
            "Последняя сессия: —"
        )

        self.last_session_label.setWordWrap(True)
        self.last_session_label.setStyleSheet(
            "color: #8B9AA5;"
        )

        group_layout.addWidget(
            self.last_session_label
        )

        layout.addWidget(
            group
        )

        layout.addStretch()

        return tab

    # ========================================================
    # SETTINGS TAB (настройки записи)
    # ========================================================

    def _field_caption(self, text):
        # Подпись поля (серая, над элементом) — как в
        # create_measurement_tab.
        caption = QLabel(text)
        caption.setStyleSheet(
            "color: #8B9AA5;"
        )
        return caption

    def create_settings_tab(self):

        tab = QWidget()

        layout = QVBoxLayout(tab)

        layout.setContentsMargins(
            5, 5, 5, 5
        )

        layout.setSpacing(8)

        record_group = QGroupBox(
            "ЗАПИСЬ ДАННЫХ"
        )

        form = QGridLayout(
            record_group
        )

        form.setContentsMargins(
            10, 10, 10, 10
        )

        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)

        # --- Имя образца ---
        name_stack = QVBoxLayout()
        name_stack.setSpacing(4)

        name_stack.addWidget(
            self._field_caption("Имя образца")
        )

        self.specimen_name_edit = QLineEdit()
        self.specimen_name_edit.setText("Образец-1")
        self.specimen_name_edit.setMaximumWidth(260)

        name_stack.addWidget(
            self.specimen_name_edit
        )

        form.addLayout(
            name_stack, 0, 0
        )

        # --- Интервал записи ---
        interval_stack = QVBoxLayout()
        interval_stack.setSpacing(4)

        interval_stack.addWidget(
            self._field_caption("Интервал записи")
        )

        self.record_interval_edit = QLineEdit()
        self.record_interval_edit.setText("30м")
        self.record_interval_edit.setMaximumWidth(160)

        interval_stack.addWidget(
            self.record_interval_edit
        )

        self.interval_hint_label = QLabel("")
        self.interval_hint_label.setStyleSheet(
            "color: #8B9AA5;"
        )

        interval_stack.addWidget(
            self.interval_hint_label
        )

        form.addLayout(
            interval_stack, 0, 1
        )

        interval_note = QLabel(
            "Формат: <число><суффикс> — с/сек/s, м/мин/m, ч/h, д/d "
            "(без суффикса — минуты)."
        )

        interval_note.setStyleSheet(
            "color: #8B9AA5;"
        )

        interval_note.setWordWrap(True)

        form.addWidget(
            interval_note, 1, 0, 1, 2
        )

        # --- Путь сохранения ---
        path_stack = QVBoxLayout()
        path_stack.setSpacing(4)

        path_stack.addWidget(
            self._field_caption("Путь сохранения")
        )

        path_row = QHBoxLayout()
        path_row.setSpacing(6)

        self.save_path_edit = QLineEdit()
        self.save_path_edit.setText(
            str(REPO_ROOT / "Saved data")
        )

        self.browse_path_button = QPushButton(
            "Обзор…"
        )

        self.browse_path_button.clicked.connect(
            self.browse_save_path
        )

        path_row.addWidget(
            self.save_path_edit
        )

        path_row.addWidget(
            self.browse_path_button
        )

        path_stack.addLayout(
            path_row
        )

        form.addLayout(
            path_stack, 2, 0, 1, 2
        )

        self.record_path_hint_label = QLabel(
            "Папка сессии создаётся при старте измерения: "
            "«ИмяОбразца_дд-мм-гггг_ЧЧ-ММ-СС»."
        )

        self.record_path_hint_label.setStyleSheet(
            "color: #8B9AA5;"
        )

        self.record_path_hint_label.setWordWrap(True)

        form.addWidget(
            self.record_path_hint_label, 3, 0, 1, 2
        )

        layout.addWidget(
            record_group
        )

        # --- Траверса: границы и стартовая позиция ---
        traverse_group = QGroupBox(
            "ТРАВЕРСА"
        )

        traverse_form = QGridLayout(
            traverse_group
        )

        traverse_form.setContentsMargins(
            10, 10, 10, 10
        )

        traverse_form.setHorizontalSpacing(8)
        traverse_form.setVerticalSpacing(4)

        traverse_limit_stack = QVBoxLayout()
        traverse_limit_stack.setSpacing(4)

        traverse_limit_stack.addWidget(
            self._field_caption("Верхняя граница, мм")
        )

        self.traverse_max_edit = QLineEdit()
        self.traverse_max_edit.setText("145")
        self.traverse_max_edit.setMaximumWidth(120)

        traverse_limit_stack.addWidget(
            self.traverse_max_edit
        )

        traverse_form.addLayout(
            traverse_limit_stack, 0, 0
        )

        traverse_start_stack = QVBoxLayout()
        traverse_start_stack.setSpacing(4)

        traverse_start_stack.addWidget(
            self._field_caption("Стартовая позиция, мм")
        )

        self.traverse_start_edit = QLineEdit()
        self.traverse_start_edit.setText("10")
        self.traverse_start_edit.setMaximumWidth(120)

        traverse_start_stack.addWidget(
            self.traverse_start_edit
        )

        traverse_form.addLayout(
            traverse_start_stack, 0, 1
        )

        traverse_speed_stack = QVBoxLayout()
        traverse_speed_stack.setSpacing(4)

        traverse_speed_stack.addWidget(
            self._field_caption("Скорость выхода на старт, мм/с")
        )

        self.traverse_start_speed_edit = QLineEdit()
        self.traverse_start_speed_edit.setText("0.5")
        self.traverse_start_speed_edit.setMaximumWidth(120)

        traverse_speed_stack.addWidget(
            self.traverse_start_speed_edit
        )

        traverse_form.addLayout(
            traverse_speed_stack, 0, 2
        )

        # --- Стопор по силе (защита датчика) ---
        force_stop_stack = QVBoxLayout()
        force_stop_stack.setSpacing(4)

        force_stop_stack.addWidget(
            self._field_caption("Стопор по силе, Н")
        )

        self.force_stop_edit = QLineEdit()
        self.force_stop_edit.setText("-5")
        self.force_stop_edit.setMaximumWidth(120)

        force_stop_stack.addWidget(
            self.force_stop_edit
        )

        traverse_form.addLayout(
            force_stop_stack, 1, 0
        )

        # --- Отвод при стопоре силы (мм вверх от текущей позиции) ---
        force_rise_stack = QVBoxLayout()
        force_rise_stack.setSpacing(4)

        force_rise_stack.addWidget(
            self._field_caption("Отвод при стопоре, мм")
        )

        self.force_stop_rise_edit = QLineEdit()
        self.force_stop_rise_edit.setText("5")
        self.force_stop_rise_edit.setMaximumWidth(120)

        force_rise_stack.addWidget(
            self.force_stop_rise_edit
        )

        traverse_form.addLayout(
            force_rise_stack, 1, 1
        )

        # --- Калибровка траверсы: HOME_0_YYY ---
        self.home_button = QPushButton(
            "Калибровка"
        )

        self.home_button.setMaximumWidth(120)

        self.home_button.clicked.connect(
            self.open_home_calibration_dialog
        )

        traverse_form.addWidget(
            self.home_button, 1, 2
        )

        traverse_note = QLabel(
            "После подключения траверса выходит на стартовую "
            "позицию; цель MOVE ограничивается верхней границей. "
            "При силе ≤ «Стопор по силе» (упор в нижний концевик) "
            "траверса быстро отводится вверх на «Отвод при "
            "стопоре» мм, всё останавливается. Превышение верхней "
            "границы хода или порога силы показывает аварийное окно."
        )

        traverse_note.setStyleSheet(
            "color: #8B9AA5;"
        )

        traverse_note.setWordWrap(True)

        traverse_form.addWidget(
            traverse_note, 2, 0, 1, 3
        )

        layout.addWidget(
            traverse_group
        )

        layout.addStretch()

        # --- Сохранение при изменении ---
        self.specimen_name_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.save_path_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.traverse_max_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.traverse_start_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.traverse_start_speed_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.force_stop_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.force_stop_rise_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.record_interval_edit.textChanged.connect(
            self.on_record_interval_changed
        )

        self.record_interval_edit.editingFinished.connect(
            self.save_app_settings
        )

        # Первичная расшифровка интервала.
        self.on_record_interval_changed(
            self.record_interval_edit.text()
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

        # ====================================================
        # ПАРАМЕТРЫ ОБРАЗЦА
        # ====================================================
        # Ширина и толщина (мм) → площадь сечения (мм2).

        specimen_group = CollapsibleGroupBox(
            "ПАРАМЕТРЫ ОБРАЗЦА"
        )

        specimen_form = QGridLayout(
            specimen_group
        )

        specimen_form.setContentsMargins(
            8, 8, 8, 8
        )

        specimen_form.setHorizontalSpacing(6)

        specimen_form.setVerticalSpacing(4)

        # Поля — стеками «подпись над полем».
        specimen_row = QHBoxLayout()
        specimen_row.setSpacing(14)

        width_stack = QVBoxLayout()
        width_stack.setSpacing(4)

        width_caption = QLabel("Ширина, мм")
        width_caption.setStyleSheet(
            "color: #8B9AA5;"
        )

        width_stack.addWidget(
            width_caption
        )

        self.specimen_width_edit = QLineEdit()
        self.specimen_width_edit.setPlaceholderText("мм")
        self.specimen_width_edit.setText("")
        self.specimen_width_edit.setFixedWidth(90)

        width_stack.addWidget(
            self.specimen_width_edit
        )

        thickness_stack = QVBoxLayout()
        thickness_stack.setSpacing(4)

        thickness_caption = QLabel("Толщина, мм")
        thickness_caption.setStyleSheet(
            "color: #8B9AA5;"
        )

        thickness_stack.addWidget(
            thickness_caption
        )

        self.specimen_thickness_edit = QLineEdit()
        self.specimen_thickness_edit.setPlaceholderText("мм")
        self.specimen_thickness_edit.setText("")
        self.specimen_thickness_edit.setFixedWidth(90)

        thickness_stack.addWidget(
            self.specimen_thickness_edit
        )

        specimen_row.addLayout(
            width_stack
        )

        specimen_row.addLayout(
            thickness_stack
        )

        specimen_row.addStretch()

        specimen_form.addLayout(
            specimen_row,
            0,
            0,
            1,
            2
        )

        self.specimen_area_label = QLabel(
            "Площадь: — мм²"
        )

        self.specimen_area_label.setStyleSheet(
            "color: #8B9AA5;"
        )

        specimen_form.addWidget(
            self.specimen_area_label,
            1,
            0,
            1,
            2
        )

        self.specimen_width_edit.editingFinished.connect(
            self.update_specimen_area
        )

        self.specimen_thickness_edit.editingFinished.connect(
            self.update_specimen_area
        )

        self.specimen_width_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.specimen_thickness_edit.editingFinished.connect(
            self.save_app_settings
        )

        left.addWidget(
            specimen_group
        )

        # ====================================================
        # ЦЕЛЬ ЦИКЛА
        # ====================================================
        # Оценка качества цикла по литературе: σ_max, R → σ_min
        # (= R × σ_max). Значения интерпретируются в текущих
        # единицах отображения (Н/МПа); внутри хранятся в Н.
        # Обычный QGroupBox (не сворачивается).

        cycle_target_group = QGroupBox(
            "ЦЕЛЬ ЦИКЛА"
        )

        cycle_target_form = QGridLayout(
            cycle_target_group
        )

        cycle_target_form.setContentsMargins(
            8, 8, 8, 8
        )

        cycle_target_form.setHorizontalSpacing(6)

        cycle_target_form.setVerticalSpacing(4)

        # Поля — стеками «подпись над полем», как у соседей.
        cycle_target_row = QHBoxLayout()
        cycle_target_row.setSpacing(14)

        sigma_max_stack = QVBoxLayout()
        sigma_max_stack.setSpacing(4)

        sigma_max_caption = QLabel("Sigma_max")
        sigma_max_caption.setStyleSheet(
            "color: #8B9AA5;"
        )

        sigma_max_stack.addWidget(
            sigma_max_caption
        )

        self.cycle_target_sigma_max_edit = QLineEdit()
        self.cycle_target_sigma_max_edit.setPlaceholderText("Н/МПа")
        self.cycle_target_sigma_max_edit.setText("")
        self.cycle_target_sigma_max_edit.setFixedWidth(90)

        sigma_max_stack.addWidget(
            self.cycle_target_sigma_max_edit
        )

        r_stack = QVBoxLayout()
        r_stack.setSpacing(4)

        r_caption = QLabel("R")
        r_caption.setStyleSheet(
            "color: #8B9AA5;"
        )

        r_stack.addWidget(
            r_caption
        )

        self.cycle_target_r_edit = QLineEdit()
        self.cycle_target_r_edit.setPlaceholderText("коэффициент")
        self.cycle_target_r_edit.setText("")
        self.cycle_target_r_edit.setFixedWidth(90)

        r_stack.addWidget(
            self.cycle_target_r_edit
        )

        cycle_target_row.addLayout(
            sigma_max_stack
        )

        cycle_target_row.addLayout(
            r_stack
        )

        cycle_target_row.addStretch()

        cycle_target_form.addLayout(
            cycle_target_row,
            0,
            0,
            1,
            2
        )

        # σ_min — read-only, = R × σ_max (в текущих единицах).
        sigma_min_row = QHBoxLayout()
        sigma_min_row.setSpacing(6)

        sigma_min_caption = QLabel("Sigma_min")
        sigma_min_caption.setStyleSheet(
            "color: #8B9AA5;"
        )

        sigma_min_row.addWidget(
            sigma_min_caption
        )

        self.cycle_target_sigma_min_label = QLabel("—")
        self.cycle_target_sigma_min_label.setStyleSheet(
            "color: #19E6FF; font-weight: bold;"
        )

        sigma_min_row.addWidget(
            self.cycle_target_sigma_min_label
        )

        sigma_min_row.addStretch()

        cycle_target_form.addLayout(
            sigma_min_row,
            1,
            0,
            1,
            2
        )

        # Тумблер показа целевых линий (дефолт — включён).
        self.cycle_target_lines_check = ToggleSwitch(
            "Целевые линии"
        )

        self.cycle_target_lines_check.setChecked(
            True
        )

        self.cycle_target_lines_check.setToolTip(
            "Показывать на графике стационарные линии цели "
            "цикла SIG_MAX / SIG_M / SIG_MIN"
        )

        # Кнопка «σ_M → цель»: вносит середину целевого цикла
        # (σ_max/σ_min) в поле цели поддержания силы. Только
        # заполняет поле — поддержание оператор стартует сам.
        self.cycle_target_to_maintain_button = QPushButton(
            "σ_M → цель"
        )

        self.cycle_target_to_maintain_button.setMaximumWidth(120)

        self.cycle_target_to_maintain_button.setToolTip(
            "Внести σ_M (середину целевого цикла "
            "SIG_MAX/SIG_MIN) в поле цели поддержания силы"
        )

        self.cycle_target_to_maintain_button.setStyleSheet(
            "QPushButton {"
            "background: #52616C; "
            "border: 1px solid #33404A; "
            "border-radius: 4px; "
            "color: #DCE5EA; "
            "font-weight: bold; "
            "padding: 6px 10px;"
            "}"
        )

        self.cycle_target_to_maintain_button.clicked.connect(
            self.apply_cycle_target_to_maintain
        )

        # Тумблер линий и кнопка σ_M — в одном ряду.
        cycle_target_lines_row = QHBoxLayout()
        cycle_target_lines_row.setSpacing(8)

        cycle_target_lines_row.addWidget(
            self.cycle_target_lines_check
        )

        cycle_target_lines_row.addWidget(
            self.cycle_target_to_maintain_button
        )

        cycle_target_lines_row.addStretch()

        cycle_target_form.addLayout(
            cycle_target_lines_row,
            2,
            0,
            1,
            2
        )

        # Обновление σ_min и линий; сохранение настроек.
        self.cycle_target_sigma_max_edit.editingFinished.connect(
            self._update_cycle_targets
        )

        self.cycle_target_r_edit.editingFinished.connect(
            self._update_cycle_targets
        )

        self.cycle_target_lines_check.toggled.connect(
            self._update_cycle_targets
        )

        self.cycle_target_sigma_max_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.cycle_target_r_edit.editingFinished.connect(
            self.save_app_settings
        )

        self.cycle_target_lines_check.toggled.connect(
            self.save_app_settings
        )

        left.addWidget(
            cycle_target_group
        )

        # ====================================================
        # УПРАВЛЕНИЕ
        # ====================================================
        # Единый блок: старт/стоп измерения, текущие значения,
        # управление траверсой и поддержание силы. Не
        # сворачивается. Поля — стеками «подпись над полем»,
        # строками по смыслу.

        control_group = QGroupBox(
            "УПРАВЛЕНИЕ"
        )

        # Размер строго по содержимому (ширина и высота).
        control_group.setSizePolicy(
            QSizePolicy.Maximum,
            QSizePolicy.Maximum,
        )

        control_layout = QVBoxLayout(
            control_group
        )

        control_layout.setContentsMargins(
            10, 10, 10, 10
        )

        control_layout.setSpacing(8)

        # Подпись поля (серая, над элементом).
        def _field_caption(text):
            caption = QLabel(text)
            caption.setStyleSheet(
                "color: #8B9AA5;"
            )
            return caption

        # ----------------------------------------------------
        # Старт / стоп измерения.
        # ----------------------------------------------------

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

        measure_row = QHBoxLayout()
        measure_row.setSpacing(6)

        measure_row.addWidget(
            self.start_button
        )

        measure_row.addWidget(
            self.stop_button
        )

        measure_row.addStretch()

        control_layout.addLayout(
            measure_row
        )

        # ----------------------------------------------------
        # Текущие значения: сила / напряжение и положение
        # траверсы — два стека.
        # ----------------------------------------------------

        self.measurement_force_label = QLabel(
            "0.000 N"
        )

        self.measurement_force_label.setAlignment(
            Qt.AlignCenter
        )

        self.measurement_force_label.setMinimumHeight(
            55
        )

        self.measurement_force_label.setFixedWidth(200)

        self.measurement_force_label.setStyleSheet(
            "background: #08151A; "
            "border: 1px solid #1C7F90; "
            "border-radius: 5px; "
            "color: #19E6FF; "
            "font-size: 23pt; "
            "font-weight: bold; "
            "padding: 4px;"
        )

        # Кнопки режима единиц (Н/МПа, взаимоисключающие):
        # активная — бирюзовая, неактивная — серая.
        self.force_units_n_button = QPushButton("Н")
        self.force_units_n_button.setCheckable(True)
        self.force_units_n_button.setFixedWidth(52)
        self.force_units_n_button.clicked.connect(
            lambda: self.set_force_display_units("N")
        )

        self.force_units_mpa_button = QPushButton("МПа")
        self.force_units_mpa_button.setCheckable(True)
        self.force_units_mpa_button.setFixedWidth(64)
        self.force_units_mpa_button.clicked.connect(
            lambda: self.set_force_display_units("MPa")
        )

        self.force_display_units = "N"
        self._update_force_units_buttons()

        force_stack = QVBoxLayout()
        force_stack.setSpacing(4)

        force_stack.addWidget(
            _field_caption("Сила / Напряжение")
        )

        force_row = QHBoxLayout()
        force_row.setSpacing(6)

        # Кнопки в линию с окном вывода, по центру по высоте.
        for _btn in (
                self.force_units_n_button,
                self.force_units_mpa_button,
        ):
            _btn_col = QVBoxLayout()
            _btn_col.addStretch()
            _btn_col.addWidget(_btn)
            _btn_col.addStretch()
            force_row.addLayout(_btn_col)

        force_row.addWidget(
            self.measurement_force_label
        )

        force_row.addStretch()

        force_stack.addLayout(
            force_row
        )

        self.traverse_position_label = QLabel(
            "0.000 mm"
        )

        self.traverse_position_label.setAlignment(
            Qt.AlignCenter
        )

        self.traverse_position_label.setMinimumHeight(
            55
        )

        self.traverse_position_label.setFixedWidth(170)

        self.traverse_position_label.setStyleSheet(
            "background: #08151A; "
            "border: 1px solid #1C7F90; "
            "border-radius: 5px; "
            "color: #19E6FF; "
            "font-size: 23pt; "
            "font-weight: bold; "
            "padding: 4px;"
        )

        position_stack = QVBoxLayout()
        position_stack.setSpacing(4)

        position_stack.addWidget(
            _field_caption("Положение траверсы, мм")
        )

        position_stack.addWidget(
            self.traverse_position_label
        )

        values_row = QHBoxLayout()
        values_row.setSpacing(14)

        values_row.addLayout(
            force_stack
        )

        values_row.addLayout(
            position_stack
        )

        values_row.addStretch()

        control_layout.addLayout(
            values_row
        )

        # ----------------------------------------------------
        # Управление траверсой: точка и скорость — стеками.
        # ----------------------------------------------------

        self.traverse_target_edit = QLineEdit()
        self.traverse_target_edit.setPlaceholderText("Точка, мм")
        self.traverse_target_edit.setText("0.000")
        self.traverse_target_edit.setFixedWidth(90)

        self.traverse_speed_edit = QLineEdit()
        self.traverse_speed_edit.setPlaceholderText("Скорость, мм/с")
        self.traverse_speed_edit.setText("0.500")
        self.traverse_speed_edit.setFixedWidth(90)

        self.traverse_move_button = QPushButton("ПЕРЕМЕСТИТЬ")
        self.traverse_move_button.clicked.connect(
            self.move_traverse_to_target
        )

        self.traverse_start_button = QPushButton("Стартовая позиция")
        self.traverse_start_button.clicked.connect(
            self.move_traverse_to_start_position
        )

        self.traverse_target_edit.editingFinished.connect(
            self.on_interactive_field_changed
        )

        self.traverse_speed_edit.editingFinished.connect(
            self.on_interactive_field_changed
        )

        target_stack = QVBoxLayout()
        target_stack.setSpacing(4)

        target_stack.addWidget(
            _field_caption("Точка, мм")
        )

        target_stack.addWidget(
            self.traverse_target_edit
        )

        speed_stack = QVBoxLayout()
        speed_stack.setSpacing(4)

        speed_stack.addWidget(
            _field_caption("Скорость, мм/с")
        )

        speed_stack.addWidget(
            self.traverse_speed_edit
        )

        move_stack = QVBoxLayout()
        move_stack.setSpacing(4)

        move_stack.addSpacing(
            22
        )

        move_stack.addWidget(
            self.traverse_move_button
        )

        start_stack = QVBoxLayout()
        start_stack.setSpacing(4)

        start_stack.addSpacing(
            22
        )

        start_stack.addWidget(
            self.traverse_start_button
        )

        traverse_row = QHBoxLayout()
        traverse_row.setSpacing(14)

        traverse_row.addLayout(
            target_stack
        )

        traverse_row.addLayout(
            speed_stack
        )

        traverse_row.addLayout(
            move_stack
        )

        traverse_row.addLayout(
            start_stack
        )

        traverse_row.addStretch()

        control_layout.addLayout(
            traverse_row
        )

        # ----------------------------------------------------
        # Поддержание силы: цель (поле + кнопки единиц),
        # скорость, окно факта, тумблер авто-скорости.
        # ----------------------------------------------------

        self.maintain_force_edit = QLineEdit()
        self.maintain_force_edit.setPlaceholderText(
            "Таргетная сила"
        )
        self.maintain_force_edit.setText("0.000")
        self.maintain_force_edit.setFixedWidth(90)

        # Кнопки единиц целевой силы поддержания: Н / МПа.
        self.maintain_units_n_button = QPushButton("Н")
        self.maintain_units_n_button.setCheckable(True)
        self.maintain_units_n_button.setFixedWidth(52)
        self.maintain_units_n_button.clicked.connect(
            lambda: self.set_maintain_units("N")
        )

        self.maintain_units_mpa_button = QPushButton("МПа")
        self.maintain_units_mpa_button.setCheckable(True)
        self.maintain_units_mpa_button.setFixedWidth(64)
        self.maintain_units_mpa_button.clicked.connect(
            lambda: self.set_maintain_units("MPa")
        )

        self.maintain_units = "N"
        self._update_maintain_units_buttons()

        self.maintain_force_edit.editingFinished.connect(
            self.on_interactive_field_changed
        )

        target_units_stack = QVBoxLayout()
        target_units_stack.setSpacing(4)

        target_units_stack.addWidget(
            _field_caption("Целевое значение")
        )

        target_units_row = QHBoxLayout()
        target_units_row.setSpacing(4)

        target_units_row.addWidget(
            self.maintain_units_n_button
        )

        target_units_row.addWidget(
            self.maintain_units_mpa_button
        )

        target_units_row.addWidget(
            self.maintain_force_edit
        )

        target_units_row.addStretch()

        target_units_stack.addLayout(
            target_units_row
        )

        self.maintain_speed_edit = QLineEdit()
        self.maintain_speed_edit.setPlaceholderText(
            "Скорость, мм/с"
        )
        self.maintain_speed_edit.setText("0.500")
        self.maintain_speed_edit.setFixedWidth(90)

        self.maintain_speed_edit.editingFinished.connect(
            self.on_interactive_field_changed
        )

        maintain_speed_stack = QVBoxLayout()
        maintain_speed_stack.setSpacing(4)

        maintain_speed_stack.addWidget(
            _field_caption("Скорость траверсы, мм/с")
        )

        maintain_speed_stack.addWidget(
            self.maintain_speed_edit
        )

        # Окно фактической скорости: стрелка направления
        # (зелёная) слева, прочерк когда перемещения нет.
        self.traverse_speed_actual_label = QLabel(
            "—"
        )

        self.traverse_speed_actual_label.setAlignment(
            Qt.AlignCenter
        )

        self.traverse_speed_actual_label.setFixedWidth(170)

        self.traverse_speed_actual_label.setStyleSheet(
            "background: #08151A; "
            "border: 1px solid #1C7F90; "
            "border-radius: 5px; "
            "color: #19E6FF; "
            "font-size: 18pt; "
            "font-weight: bold; "
            "padding: 2px;"
        )

        actual_stack = QVBoxLayout()
        actual_stack.setSpacing(4)

        actual_stack.addWidget(
            _field_caption("Факт, мм/с")
        )

        actual_stack.addWidget(
            self.traverse_speed_actual_label
        )

        # Автоматическое вычисление скорости — тумблером.
        self.maintain_auto_speed_check = ToggleSwitch(
            "Авто-скорость"
        )

        self.maintain_auto_speed_check.setToolTip(
            "Скорость каждого перемещения вычисляется "
            "пропорционально ошибке по силе "
            "(поле скорости игнорируется)"
        )

        # Авто-скорость гасит поле ручной скорости траверсы.
        self.maintain_auto_speed_check.toggled.connect(
            self.on_maintain_auto_speed_toggled
        )
        self.on_maintain_auto_speed_toggled(
            self.maintain_auto_speed_check.isChecked()
        )

        self.maintain_button = QPushButton(
            "НАЧАТЬ ПОДДЕРЖИВАТЬ"
        )
        self.maintain_button.clicked.connect(
            self.toggle_maintain_force
        )

        maintain_action_stack = QVBoxLayout()
        maintain_action_stack.setSpacing(4)

        # Выравнивание по высоте окна факта (подпись + окно).
        maintain_action_stack.addSpacing(20)

        maintain_action_stack.addWidget(
            self.maintain_auto_speed_check
        )

        maintain_action_stack.addWidget(
            self.maintain_button
        )

        # Строка полей поддержания: цель + скорость.
        maintain_fields_row = QHBoxLayout()
        maintain_fields_row.setSpacing(14)

        maintain_fields_row.addLayout(
            target_units_stack
        )

        maintain_fields_row.addLayout(
            maintain_speed_stack
        )

        maintain_fields_row.addStretch()

        control_layout.addLayout(
            maintain_fields_row
        )

        # Следующая строка: факт скорости, авто-скорость,
        # кнопка запуска поддержания.
        maintain_row = QHBoxLayout()
        maintain_row.setSpacing(14)

        maintain_row.addLayout(
            actual_stack
        )

        maintain_row.addLayout(
            maintain_action_stack
        )

        maintain_row.addStretch()

        control_layout.addLayout(
            maintain_row
        )

        control_layout.addStretch()

        self.maintain_group = control_group

        left.addWidget(
            control_group
        )


        # =====================================================
        # СТАТУС ДАННЫХ — статус-бар внизу окна (бывший блок
        # «СОСТОЯНИЕ»; ЧАСТОТА (МИН) вынесена в карточку циклов).
        # =====================================================

        def status_bar_item(caption, value_widget):
            # Постоянный элемент статус-бара: подпись + значение.
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(10, 0, 10, 0)
            holder_layout.setSpacing(6)

            caption_label = QLabel(caption)
            caption_label.setStyleSheet(
                "color: #8B9AA5; font-weight: bold;"
            )

            holder_layout.addWidget(caption_label)
            holder_layout.addWidget(value_widget)

            self.statusBar().addPermanentWidget(holder)
            return holder

        self.measurement_frame_count_label = QLabel(
            "0"
        )

        self.measurement_frame_count_label.setAlignment(
            Qt.AlignRight
        )

        status_bar_item(
            "КАДРЫ",
            self.measurement_frame_count_label,
        )

        self.measurement_time_label = QLabel(
            "0.000 s"
        )

        self.measurement_time_label.setAlignment(
            Qt.AlignRight
        )

        status_bar_item(
            "ВРЕМЯ",
            self.measurement_time_label,
        )

        self.measurement_freq_label = QLabel(
            "— Hz"
        )

        self.measurement_freq_label.setAlignment(
            Qt.AlignRight
        )

        status_bar_item(
            "ЧАСТОТА (1 С)",
            self.measurement_freq_label,
        )

        self.measurement_freq3_label = QLabel(
            "— Hz"
        )

        self.measurement_freq3_label.setAlignment(
            Qt.AlignRight
        )

        status_bar_item(
            "ЧАСТОТА (3 С)",
            self.measurement_freq3_label,
        )

        left.addStretch()

        # ====================================================
        # RIGHT — GRAPH
        # ====================================================

        graph_group = QGroupBox(
            "ГРАФИК"
        )

        graph_layout = QVBoxLayout(
            graph_group
        )

        graph_layout.setContentsMargins(
            5, 5, 5, 5
        )

        graph_layout.setSpacing(4)

        # Анализ цикла: карточки MIN/MAX/СРЕДНЕЕ/АМПЛИТУДА/
        # КОЛИЧЕСТВО — отдельный блок верхней строки.
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Анализ цикла: карточки над графиком.
        # ----------------------------------------------------

        cycle_count_layout = QHBoxLayout()

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
            # Шаблон — окно «ТЕКУЩЕЕ ЗНАЧЕНИЕ» /
            # «ПОЛОЖЕНИЕ ТРАВЕРСЫ»: тёмный фон, рамка #1C7F90,
            # крупное светлое значение; окно подсвечивается
            # цветом линии, подпись — серым как у «ТЕКУЩЕЕ ЗНАЧЕНИЕ».
            card = QFrame()
            # Компактная фиксированная высота карточки.
            card.setFixedHeight(64)
            card.setStyleSheet(
                "QFrame {"
                "background: #08151A;"
                "border: 1px solid #1C7F90;"
                "border-radius: 5px;"
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
                "color: #8B9AA5; "
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
                f"color: {color}; "
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

            # Ссылка на карточку (окно) для смены фона
            # при унификации цветов.
            value_label.card_frame = card
            value_label.base_color = color

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

        self.measurement_cpm_label = create_cycle_card(
            "ЧАСТОТА (МИН)",
            "—",
            "#39FF88",
        )

        # Карточки — окна со значением: остаются видимыми,
        # когда группа свёрнута.

        # ----------------------------------------------------
        # Верхняя строка: подключение | режим ввода | анализ
        # цикла — три блока по смыслу.
        # ----------------------------------------------------


        graph_layout.addLayout(
            cycle_count_layout
        )

        # ----------------------------------------------------
        # GRAPH
        # ----------------------------------------------------

        self.force_graph = ForceGraphWidget()

        self.force_graph.on_force_color_changed = (
            self.set_force_value_color
        )

        self.force_graph.on_mid_color_changed = (
            self.set_mean_value_color
        )

        self.force_graph.on_min_color_changed = (
            self.set_min_value_color
        )

        self.force_graph.on_max_color_changed = (
            self.set_max_value_color
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

        self.log.appendPlainText(
            f"[{timestamp}] {text}"
        )

        self.log.ensureCursorVisible()

    # ========================================================
    # CALIBRATION LOG
    # ========================================================

    def append_calibration_log(self, text):

        timestamp = self.timestamp()

        self.calibration_log.appendPlainText(
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

    def closeEvent(self, event):
        # Жизненный цикл окна — в MainWindow (не в миксине):
        # super().closeEvent() из миксина после QMainWindow
        # упирается в object и падает (AttributeError).

        # Сохранить настройки при закрытии окна.
        self.save_app_settings()

        # Дописать буфер записи, если сессия ещё активна.
        if self.session_recorder.active:
            self.session_recorder.stop_session()

        super().closeEvent(event)

# ============================================================
# APPLICATION
# ============================================================
