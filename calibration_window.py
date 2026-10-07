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
from graph_widget import ForceGraphWidget
from session_recorder import (
    SessionRecorder,
    parse_interval,
    format_interval_hint,
)
from logging_setup import logger


class ToggleSwitch(QCheckBox):
    # Тумблер с бегунком: подложка-капсула, светлый
    # круглый бегунок, при включении сдвигается вправо,
    # подложка подсвечивается.
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        text = self.text()
        track_w = 44
        track_h = 24
        track_y = (
                (self.height() - track_h) // 2
        )

        checked = self.isChecked()

        if checked:
            track_color = QColor("#1C7F90")
            track_border = QColor("#19E6FF")
        else:
            track_color = QColor("#08151A")
            track_border = QColor("#71808C")

        if self.isEnabled() and self.underMouse():
            track_border = QColor("#FFFFFF")

        painter.setPen(
            QPen(track_border, 1)
        )
        painter.setBrush(track_color)
        painter.drawRoundedRect(
            0,
            track_y,
            track_w,
            track_h,
            track_h // 2,
            track_h // 2,
        )

        # Бегунок: 3 px от края дорожки.
        margin = 3
        knob_d = track_h - 2 * margin

        if checked:
            knob_x = track_w - margin - knob_d
        else:
            knob_x = margin

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#F2F5F7"))
        painter.drawEllipse(
            knob_x,
            track_y + margin,
            knob_d,
            knob_d,
        )

        if text:
            painter.setPen(
                QPen(QColor("#F2F5F7"))
            )

            painter.drawText(
                QRectF(
                    track_w + 10,
                    0,
                    self.width() - track_w - 10,
                    self.height(),
                ),
                Qt.AlignVCenter | Qt.AlignLeft,
                text,
            )

    def sizeHint(self):
        fm = self.fontMetrics()
        width = (
                44
                + 10
                + (
                    fm.horizontalAdvance(self.text())
                    if self.text()
                    else 0
                )
                + 8
        )

        return QSize(width, max(26, fm.height() + 6))


class CollapsibleGroupBox(QGroupBox):
    # Группа-аккордеон: маркер в заголовке ("V" — развёрнута,
    # ">" — свёрнута), клик по заголовку переключает состояние.
    # Содержимое скрывается, кроме виджетов, зарегистрированных
    # через keep_visible() (окна со значением).

    TITLE_CLICK_HEIGHT = 22

    def __init__(self, title="", parent=None):
        super().__init__(title, parent)
        # Высота по содержимому: лишнее место уходит в stretch,
        # при сворачивании группа сжимается без пустот.
        self.setSizePolicy(
            QSizePolicy.Preferred,
            QSizePolicy.Maximum,
        )
        # objectName для QSS-сдвига заголовка вправо
        # (маркер рисуется поверх группы).
        self.setObjectName("collapsible")
        self._base_title = title
        self._keep = []
        self._expanded = True

    def paintEvent(self, event):
        # Маркер состояния: рамка с "V" (развёрнуто) или
        # ">" (свёрнуто) слева от заголовка, обведён рамкой,
        # чтобы выделяться из текста заголовка.
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        marker_rect = QRectF(10, 3, 18, 16)

        painter.setPen(
            QPen(QColor("#1C7F90"), 1)
        )
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            marker_rect,
            3,
            3,
        )

        painter.setPen(
            QPen(QColor("#19E6FF"))
        )

        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)

        painter.drawText(
            marker_rect,
            Qt.AlignCenter,
            "V" if self._expanded else ">",
        )

    def keep_visible(self, widget):
        self._keep.append(widget)

    def _full_title(self):
        # Маркер рисуется в paintEvent, текст заголовка —
        # без префикса.
        return self._base_title

    def mousePressEvent(self, event):
        if (
                event.button() == Qt.LeftButton
                and event.pos().y() <= self.TITLE_CLICK_HEIGHT
        ):
            self.toggle_collapsed()
        super().mousePressEvent(event)

    def toggle_collapsed(self):
        self._expanded = not self._expanded
        self.setTitle(
            self._full_title()
        )
        self._apply_state()

    def _apply_state(self):
        def walk(layout):
            for i in range(layout.count()):
                item = layout.itemAt(i)
                widget = item.widget()
                if widget is not None:
                    widget.setVisible(
                        self._expanded
                        or widget in self._keep
                    )
                else:
                    sub = item.layout()
                    if sub is not None:
                        walk(sub)
        lay = self.layout()
        if lay is not None:
            walk(lay)


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
        self.cycle_times = []
        self.last_cycle_count = 0
        self.cpm_history = []
        self.live_min_force = None
        self.live_max_force = None
        self.speed_samples = []
        self.measured_speed_mm_s = None
        self.measurement_start_time = None

        # ----------------------------------------------------
        # UI
        # ----------------------------------------------------

        # Запись сессии испытания (этап 0).
        self.session_recorder = SessionRecorder(self)

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

        # Пересчитать карточки.
        self.update_measurement_info()

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

        self.calibration_log = QPlainTextEdit()
        self.calibration_log.setReadOnly(
            True
        )
        self.calibration_log.setMaximumBlockCount(2000)

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

        # --- Калибровка траверсы: HOME_0_YYY ---
        self.home_button = QPushButton(
            "Калибровка"
        )

        self.home_button.setMaximumWidth(120)

        self.home_button.clicked.connect(
            self.send_home
        )

        traverse_form.addWidget(
            self.home_button, 1, 2
        )

        traverse_note = QLabel(
            "После подключения траверса выходит на стартовую "
            "позицию; цель MOVE ограничивается верхней границей. "
            "При силе ≤ «Стопор по силе» (упор в нижний концевик) "
            "траверса уходит в стартовую позицию, всё "
            "останавливается. Превышение верхней границы хода "
            "или порога силы показывает аварийное окно."
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
    # APP SETTINGS INI
    # ========================================================

    def browse_save_path(self):

        directory = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку для сохранения сессий",
            self.save_path_edit.text() or str(REPO_ROOT),
        )

        if directory:
            self.save_path_edit.setText(directory)
            self.save_app_settings()

    def on_record_interval_changed(self, text):

        hint = format_interval_hint(text)

        if parse_interval(text) is None:
            self.interval_hint_label.setStyleSheet(
                "color: #FF4D4D;"
            )
        else:
            self.interval_hint_label.setStyleSheet(
                "color: #8B9AA5;"
            )

        self.interval_hint_label.setText(hint)

        # Интервал читается live: пересоздать таймер flush.
        seconds = parse_interval(text)

        if seconds is not None:
            self.session_recorder.set_flush_interval(seconds)

    def load_app_settings(self):

        config = configparser.ConfigParser()

        if not APP_SETTINGS_PATH.exists():
            logger.info(
                f"APP SETTINGS INI not found: {APP_SETTINGS_PATH}"
            )
            return

        try:
            config.read(
                APP_SETTINGS_PATH,
                encoding="utf-8"
            )

            if not config.has_section("RECORDING"):
                return

            section = config["RECORDING"]

            self.specimen_name_edit.setText(
                section.get("specimen_name", fallback="Образец-1")
            )

            self.save_path_edit.setText(
                section.get(
                    "save_path",
                    fallback=str(REPO_ROOT / "Saved data"),
                )
            )

            # Сначала применяем значения из файла во ВСЕ поля,
            # включая траверсу: on_record_interval_changed ниже
            # вызывает save_app_settings, и он записал бы в файл
            # ещё не загруженные дефолты полей траверсы.
            if config.has_section("TRAVERSE"):

                traverse = config["TRAVERSE"]

                self.traverse_max_edit.setText(
                    traverse.get("max_mm", fallback="145")
                )

                self.traverse_start_edit.setText(
                    traverse.get("start_mm", fallback="10")
                )

                self.traverse_start_speed_edit.setText(
                    traverse.get("start_speed_mm_s", fallback="0.5")
                )

                self.force_stop_edit.setText(
                    traverse.get("force_stop_n", fallback="-5")
                )

            self.record_interval_edit.setText(
                section.get("record_interval", fallback="30м")
            )

            # textChanged мог не сработать при установке
            # идентичного текста — обновим зависимые элементы.
            self.on_record_interval_changed(
                self.record_interval_edit.text()
            )

        except Exception as e:

            logger.error(f"APP SETTINGS load ERROR: {e}")

    def save_app_settings(self):

        config = configparser.ConfigParser()

        if APP_SETTINGS_PATH.exists():
            config.read(
                APP_SETTINGS_PATH,
                encoding="utf-8"
            )

        if not config.has_section("RECORDING"):
            config.add_section("RECORDING")

        config["RECORDING"]["specimen_name"] = (
            self.specimen_name_edit.text()
        )

        config["RECORDING"]["record_interval"] = (
            self.record_interval_edit.text()
        )

        config["RECORDING"]["save_path"] = (
            self.save_path_edit.text()
        )

        if not config.has_section("TRAVERSE"):
            config.add_section("TRAVERSE")

        config["TRAVERSE"]["max_mm"] = (
            self.traverse_max_edit.text()
        )

        config["TRAVERSE"]["start_mm"] = (
            self.traverse_start_edit.text()
        )

        config["TRAVERSE"]["start_speed_mm_s"] = (
            self.traverse_start_speed_edit.text()
        )

        config["TRAVERSE"]["force_stop_n"] = (
            self.force_stop_edit.text()
        )

        # Устаревший ключ (поле «Подъём при стопоре» удалено).
        config["TRAVERSE"].pop("force_stop_rise_mm", None)

        try:
            with open(
                    APP_SETTINGS_PATH,
                    "w",
                    encoding="utf-8"
            ) as f:
                config.write(f)

        except Exception as e:

            logger.error(f"APP SETTINGS save ERROR: {e}")

    def closeEvent(self, event):

        # Сохранить настройки при закрытии окна.
        self.save_app_settings()

        # Дописать буфер записи, если сессия ещё активна.
        if self.session_recorder.active:
            self.session_recorder.stop_session()

        super().closeEvent(event)

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

        left.addWidget(
            specimen_group
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
        # СТАТУС ДАННЫХ
        # =====================================================

        status_group = CollapsibleGroupBox(
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

        status_layout.addWidget(
            QLabel("ЧАСТОТА (1 С)"),
            2,
            0
        )

        self.measurement_freq_label = QLabel(
            "— Hz"
        )

        self.measurement_freq_label.setAlignment(
            Qt.AlignRight
        )

        status_layout.addWidget(
            self.measurement_freq_label,
            2,
            1
        )

        status_layout.addWidget(
            QLabel("ЧАСТОТА (3 С)"),
            3,
            0
        )

        self.measurement_freq3_label = QLabel(
            "— Hz"
        )

        self.measurement_freq3_label.setAlignment(
            Qt.AlignRight
        )

        status_layout.addWidget(
            self.measurement_freq3_label,
            3,
            1
        )

        status_layout.addWidget(
            QLabel("ЧАСТОТА (МИН)"),
            4,
            0
        )

        self.measurement_cpm_label = QLabel(
            "—"
        )

        self.measurement_cpm_label.setAlignment(
            Qt.AlignRight
        )

        status_layout.addWidget(
            self.measurement_cpm_label,
            4,
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

    # ========================================================
    # TRAVERSE LIMITS (настройки, вкладка «Настройки»)
    # ========================================================

    def get_traverse_max_mm(self):

        try:
            return float(
                self.traverse_max_edit.text().replace(",", ".")
            )
        except ValueError:
            return 145.0

    def get_traverse_start_mm(self):

        try:
            return float(
                self.traverse_start_edit.text().replace(",", ".")
            )
        except ValueError:
            return 10.0

    def get_traverse_start_speed(self):

        try:
            speed = float(
                self.traverse_start_speed_edit.text().replace(",", ".")
            )
        except ValueError:
            speed = 0.5

        return max(0.001, speed)

    def get_force_stop_n(self):
        # Порог стопора по силе (Н): отрицательное число — упор
        # в нижний концевик. None = стопор отключён.
        try:
            value = float(
                self.force_stop_edit.text().replace(",", ".")
            )
        except ValueError:
            return None

        return value if value < 0 else None

    # ----------------------------------------------------
    # Аварийное окно (красное, привлекающее внимание)
    # ----------------------------------------------------

    def show_emergency_dialog(
            self,
            reason,
            force_n,
            current_mm,
    ):
        # Красное окно ошибки: причина, показания датчика силы
        # и позиции траверсы на момент срабатывания. Модальное:
        # пока открыто, новые триггеры не наслаиваются
        # (кадры приходят и во время диалога).
        if getattr(self, "_emergency_dialog_open", False):
            return

        self._emergency_dialog_open = True

        try:
            suffix = self.current_force_suffix()
            force_text = (
                f"{self.convert_force_value(force_n):.3f} {suffix}"
                if force_n is not None
                else "—"
            )
            pos_text = (
                f"{current_mm:.3f} мм"
                if current_mm is not None
                else "—"
            )

            box = QMessageBox(self)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("АВАРИЙНАЯ ОСТАНОВКА")
            box.setStyleSheet(
                "QMessageBox { background: #7B1010; }"
                "QMessageBox QLabel { color: #FFFFFF; "
                "font-size: 13pt; font-weight: bold; }"
                "QMessageBox QPushButton { background: #FFFFFF; "
                "color: #7B1010; font-weight: bold; "
                "min-width: 120px; padding: 6px; }"
            )
            box.setText(
                "АВАРИЙНАЯ ОСТАНОВКА\n\n"
                f"Причина: {reason}\n\n"
                f"Сила: {force_text}\n"
                f"Положение траверсы: {pos_text}"
            )
            box.setStandardButtons(QMessageBox.Ok)
            box.exec_()
        finally:
            self._emergency_dialog_open = False

    def _emergency_stop_all(self):
        # Остановить поддержание, измерение и запись.
        if self.maintain_active:
            self.maintain_active = False
            self.maintain_caught = False
            self.maintain_window = []
            self.maintain_timer.stop()
            self.maintain_button.setText(
                "НАЧАТЬ ПОДДЕРЖИВАТЬ"
            )
            self.set_traverse_block_enabled(True)
            logger.info("MAINTAIN: остановлено (аварийный стопор)")

        if self.measurement_running:
            self.send_command(STOP_COMMAND)
            self.measurement_running = False
            self.force_graph.stop_measurement()
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)

        if self.session_recorder.active:
            self.session_recorder.stop_session()
            self.last_session_label.setText(
                f"Последняя сессия: "
                f"{self.session_recorder.session_dir}"
            )
            self._set_record_button_state()

    def check_force_stop(self, force_n, current_mm):
        # Стопор по силе: сила ≤ порога означает упор в нижний
        # концевик — датчик и образец под угрозой. Окно и действия
        # — при ДОСТИЖЕНИИ условия (переход «норма → нарушение»):
        # держится нарушение — окно не повторяется; сила вернулась
        # выше порога — защита перезаряжается и следующее
        # достижение снова покажет окно.
        stop_n = self.get_force_stop_n()

        violating = (
            stop_n is not None
            and force_n <= stop_n
        )

        if not violating:
            # Норма — защита снова готова к следующему достижению.
            self._force_stop_active = False
            return

        if getattr(self, "_force_stop_active", False):
            # Нарушение уже обрабатывалось и ещё не отпустило.
            return

        self._force_stop_active = True

        logger.critical(
            f"FORCE STOP: сила {force_n:.3f} N ≤ порога {stop_n:.3f} N "
            f"(нижний концевик), позиция {current_mm:.3f} мм"
        )

        # 1) Остановить регулятор поддержания (его MOVE мог бы
        #    перебить аварийное перемещение).
        if self.maintain_active:
            self.maintain_active = False
            self.maintain_caught = False
            self.maintain_window = []
            self.maintain_timer.stop()
            self.maintain_button.setText(
                "НАЧАТЬ ПОДДЕРЖИВАТЬ"
            )
            self.set_traverse_block_enabled(True)
            logger.info("MAINTAIN: остановлено (стопор по силе)")

        # 2) Увод траверсы в стартовую позицию (настройки),
        #    не выше верхней границы хода. Скорость — 5 мм/с.
        target_mm = min(
            self.get_traverse_start_mm(),
            self.get_traverse_max_mm(),
        )

        self.send_command(
            f"MOVE_0_{target_mm:.6f}_"
            f"{self.MAINTAIN_AUTO_SPEED_MAX:.6f}_YYY"
        )

        logger.critical(
            f"FORCE STOP: уход в стартовую позицию MOVE до "
            f"{target_mm:.3f} мм"
        )

        self.append_log(
            f"СТОПОР ПО СИЛЕ: {force_n:.3f} N ≤ {stop_n:.3f} N — "
            f"упор в нижний концевик. Траверса уходит в стартовую "
            f"позицию ({target_mm:.1f} мм), всё остановлено."
        )

        # 3) Остановить измерение и запись сессии.
        self._emergency_stop_all()

        logger.critical(
            "FORCE STOP: измерение, поддержание и запись остановлены"
        )

        # 4) Аварийное окно (последним — значения уже в логе).
        self.show_emergency_dialog(
            f"Сила {force_n:.3f} Н ниже порога {stop_n:.3f} Н — "
            f"упор в нижний концевик (стопор по силе). "
            f"Траверса отправлена в стартовую позицию.",
            force_n,
            current_mm,
        )

    def check_traverse_limit(self, force_n, current_mm):
        # Превышение верхней границы хода траверсы (позиция из
        # кадра больше «Верхняя граница, мм»). Окно и останов —
        # при ДОСТИЖЕНИИ условия: пока позиция выше границы,
        # окно не повторяется; позиция вернулась в норму —
        # защита перезаряжается.
        limit_mm = self.get_traverse_max_mm()

        violating = (
            current_mm is not None
            and current_mm > limit_mm
        )

        if not violating:
            self._traverse_limit_active = False
            return

        if getattr(self, "_traverse_limit_active", False):
            return

        self._traverse_limit_active = True

        logger.critical(
            f"TRAVERSE LIMIT: позиция {current_mm:.3f} мм выше "
            f"верхней границы {limit_mm:.1f} мм, сила "
            f"{force_n:.3f} N"
        )

        self.append_log(
            f"АВАРИЯ: позиция траверсы {current_mm:.3f} мм выше "
            f"верхней границы {limit_mm:.1f} мм. Движение "
            f"остановлено."
        )

        self._emergency_stop_all()

        self.show_emergency_dialog(
            f"Положение траверсы {current_mm:.3f} мм превысило "
            f"верхнюю границу {limit_mm:.1f} мм. Движение "
            f"остановлено.",
            force_n,
            current_mm,
        )

    def move_traverse_to_start_position(self):
        # Выход траверсы на стартовую позицию после подключения
        # к устройству (позиция/скорость/граница — из «Настроек»).
        limit = self.get_traverse_max_mm()
        target = min(self.get_traverse_start_mm(), limit)
        speed = self.get_traverse_start_speed()

        if target <= 0:
            return

        self.send_command(
            f"MOVE_0_{target:.6f}_{speed:.6f}_YYY"
        )

        logger.info(
            f"TRAVERSE: выход на стартовую позицию "
            f"{target:.3f} мм, скорость {speed:.3f} мм/с"
        )

    def send_home(self):
        # Калибровка траверсы: возврат в домашнюю позицию.
        if not self.connected or self.serial is None:
            self.append_log(
                "ОШИБКА: нет подключения к устройству"
            )
            return

        self.send_command(HOME_COMMAND)
        self.append_log("HOME: калибровка траверсы запущена")
        logger.info("TRAVERSE: HOME (калибровка) отправлена")

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

        if self.serial is not None:

            try:
                self.serial.close()
            except Exception:
                pass

        self.serial = None
        self.connected = False
        logger.info("DISCONNECTED")

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
            logger.debug(f"SND FAIL (нет подключения): {command}")
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

            logger.debug(f"SND {command}")

            return True

        except Exception as e:

            logger.exception(f"SND FAIL: {e}")
            self.append_log(f"TX ERROR: {e}")

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

            logger.exception(f"RCV FAIL: {e}")
            self.append_log(f"RX ERROR: {e}")

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

        self.measurement_force_label.setText(
            f"{self.convert_force_value(force_n):.3f} "
            f"{self.current_force_suffix()}"
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
            force_n
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

        self.cycle_times = []
        self.last_cycle_count = 0
        self.cpm_history = []
        self.live_min_force = None
        self.live_max_force = None

        self.measurement_force_label.setText(
            "0.000 N"
        )

        self.measurement_frame_count_label.setText(
            "0"
        )

        self.measurement_time_label.setText(
            "0.000 s"
        )

        self.measurement_freq_label.setText(
            "— Hz"
        )

        self.measurement_freq3_label.setText(
            "— Hz"
        )

        self.measurement_cpm_label.setText(
            "—"
        )

        self.force_graph.clear()

        self.send_command(
            START_COMMAND
        )

        # Запись сессии: папка создаётся при старте измерения.
        # Если запись уже была активна — завершаем её: каждая
        # сессия пишет в СВОЮ новую папку.
        if self.session_recorder.active:
            self.session_recorder.stop_session()

        try:
            session_dir = self.session_recorder.start_session(
                self.save_path_edit.text(),
                self.specimen_name_edit.text(),
            )

            self.last_session_label.setText(
                f"Последняя сессия: {session_dir}"
            )

        except Exception as e:

            logger.error(f"RECORDER: не удалось начать сессию: {e}")

        self._set_record_button_state()

        logger.info("START measurement initiated")

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

        # Завершить запись сессии: дописать буфер.
        if self.session_recorder.active:
            self.session_recorder.stop_session()

        self._set_record_button_state()

        logger.info("STOP measurement initiated")

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
                f"{self.convert_force_value(self.last_force_n):.3f} "
                f"{self.current_force_suffix()}"
            )

        if self.last_current_mm is not None:
            self.traverse_position_label.setText(
                f"{self.last_current_mm:.3f} mm"
            )

        # Актуальная скорость траверсы: слева стрелка
        # направления (зелёная), прочерк когда перемещения
        # нет (меньше порога шума квантования позиции).
        speed = self.measured_speed_mm_s

        if speed is None or abs(speed) <= 0.02:
            self.traverse_speed_actual_label.setText(
                "—"
            )
        else:
            arrow = "↑" if speed > 0 else "↓"

            self.traverse_speed_actual_label.setText(
                f"<span style='color: #39FF88;'>{arrow}</span> "
                f"{abs(speed):.3f}"
            )

        self.measurement_frame_count_label.setText(
            str(self.frame_count)
        )

        # ----------------------------------------------------
        # Частота осцилляции (Гц): завершения циклов,
        # зарегистрированные в окнах 1 с и 3 с.
        # ----------------------------------------------------

        graph = self.force_graph

        now = time.time()

        count = graph.cycle_count

        if self.last_cycle_count is None:
            self.last_cycle_count = count

        elif count != self.last_cycle_count:

            completed = count - self.last_cycle_count

            if completed > 0:
                for _ in range(completed):
                    self.cycle_times.append(now)

                # Запись поцикловой строки (этап 0).
                self.record_cycle_row(now, count)

            self.last_cycle_count = count

        window_start = now - 60.0

        while (
                self.cycle_times
                and self.cycle_times[0] < window_start
        ):
            del self.cycle_times[0]

        if count == 0 and not self.cycle_times:
            self.measurement_freq_label.setText(
                "— Hz"
            )
            self.measurement_freq3_label.setText(
                "— Hz"
            )
        else:
            freq_1s = (
                    len([
                        t for t in self.cycle_times
                        if t >= now - 1.0
                    ]) / 1.0
            )

            freq_3s = (
                    len([
                        t for t in self.cycle_times
                        if t >= now - 3.0
                    ]) / 3.0
            )

            self.measurement_freq_label.setText(
                f"{freq_1s:.2f} Hz"
            )

            self.measurement_freq3_label.setText(
                f"{freq_3s:.2f} Hz"
            )

            # ЧАСТОТА (МИН): количество циклов за последнюю
            # минуту / 60. В первые 60 с знаменатель — время
            # от старта измерения, чтобы не занижать оценку.
            cycles_per_min_window = len(self.cycle_times)

            if (
                    self.measurement_start_time is not None
                    and now - self.measurement_start_time < 60.0
            ):
                denom = max(
                    1.0,
                    now - self.measurement_start_time,
                )
            else:
                denom = 60.0

            freq_min = (
                    cycles_per_min_window
                    / denom
            )

            self.measurement_cpm_label.setText(
                f"{freq_min:.2f} Hz"
            )

        # Карточки силы:
        # - во время осцилляции — по завершённым циклам
        #   (как линии MIN/MAX на графике);
        # - без осцилляции — живые экстремумы потока силы.
        # Значения — в выбранных единицах (Н/МПа).

        suffix = self.current_force_suffix()

        # Во время макро-перемещения траверсы или вне порога
        # цели циклы не оцениваются — карточки показывают
        # живые экстремумы потока.
        if (
                self.measurement_running
                and graph.cycles_evaluating
        ):
            stat_min = graph.cycle_min
            stat_max = graph.cycle_max
        else:
            stat_min = self.live_min_force
            stat_max = self.live_max_force

        if stat_min is not None:
            self.cycle_min_force_label.setText(
                f"{self.convert_force_value(stat_min):.2f} {suffix}"
            )

        if stat_max is not None:
            self.cycle_max_force_label.setText(
                f"{self.convert_force_value(stat_max):.2f} {suffix}"
            )

        if (
                stat_min is not None
                and stat_max is not None
        ):
            mean_force = (
                    stat_min
                    + stat_max
            ) / 2.0

            amplitude_force = (
                    stat_max
                    - stat_min
            ) / 2.0

            self.cycle_average_force_label.setText(
                f"{self.convert_force_value(mean_force):.2f} {suffix}"
            )

            self.cycle_amplitude_force_label.setText(
                f"{self.convert_force_value(amplitude_force):.2f} {suffix}"
            )

        self.cycle_count_label.setText(
            str(graph.cycle_count)
        )

    # ========================================================
    # SESSION RECORDING / RESET / EXPORT
    # ========================================================

    def record_cycle_row(self, now, count):
        # Добавить поцикловую строку в буфер записи. Данные — в Н;
        # слой отображения применяется только при экспорте.
        if not self.session_recorder.active:
            return

        graph = self.force_graph

        max_n = graph.cycle_max
        min_n = graph.cycle_min

        if max_n is None or min_n is None:
            return

        mid_n = graph.cycle_mid
        if mid_n is None:
            mid_n = (max_n + min_n) / 2.0

        amp_n = graph.cycle_amplitude
        if amp_n is None:
            amp_n = (max_n - min_n) / 2.0

        if self.measurement_start_time is not None:
            # Запись идёт вместе с измерением: время от START.
            elapsed = now - self.measurement_start_time
        elif self.session_recorder.session_start_time is not None:
            # Запись запущена вручную (без START измерения):
            # время от старта записи.
            elapsed = (
                now
                - self.session_recorder.session_start_time
            )
        else:
            elapsed = 0.0

        self.session_recorder.add_cycle_row({
            "elapsed_s": elapsed,
            "n": count,
            "max_n": max_n,
            "min_n": min_n,
            "mid_n": mid_n,
            "amp_n": amp_n,
        })

    def reset_session(self):
        # Кнопка «СБРОС»: сброс счётчика циклов, статистики
        # циклов и буфера записи сессии.
        reply = QMessageBox.question(
            self,
            "Сброс сессии",
            "Сбросить счётчик циклов, статистику циклов "
            "и буфер записи?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply != QMessageBox.Yes:
            return

        # Сброс детектора циклов и графика.
        self.force_graph.reset_session()

        self.cycle_times = []
        self.last_cycle_count = 0
        self.live_min_force = None
        self.live_max_force = None
        self.cpm_history = []

        # Буфер записи сессии.
        self.session_recorder.reset_buffer()

        # Карточки — сразу в исходное состояние.
        self.cycle_min_force_label.setText("—")
        self.cycle_max_force_label.setText("—")
        self.cycle_average_force_label.setText("—")
        self.cycle_amplitude_force_label.setText("—")
        self.cycle_count_label.setText("0")

        self.update_measurement_info()

        logger.info(
            "SESSION reset: счётчик циклов, статистика и "
            "буфер записи сброшены"
        )

    def _set_record_button_state(self):
        # Синхронизировать кнопку «ЗАПИСЬ» с состоянием рекордера.
        if self.session_recorder.active:
            self.record_button.setText("СТОП ЗАПИСИ")
        else:
            self.record_button.setText("СТАРТ ЗАПИСИ")

    def toggle_recording(self):
        # Ручной старт/стоп записи данных (не зависит от START/STOP
        # измерения): оператор на стенде может вести осцилляцию через
        # «Поддержание силы», не запуская измерение кнопкой START.
        if self.session_recorder.active:
            self.session_recorder.stop_session()
            self.last_session_label.setText(
                f"Последняя сессия: {self.session_recorder.session_dir}"
            )
            logger.info("RECORDER: запись остановлена вручную")
        else:
            try:
                session_dir = self.session_recorder.start_session(
                    self.save_path_edit.text(),
                    self.specimen_name_edit.text(),
                )
                self.last_session_label.setText(
                    f"Последняя сессия: {session_dir}"
                )
            except Exception as e:
                logger.error(f"RECORDER: не удалось начать сессию: {e}")
                QMessageBox.warning(
                    self,
                    "Запись",
                    f"Не удалось начать запись:\n{e}",
                )

        self._set_record_button_state()

    def build_stats_rows(self):
        # Строки карточек для отчёта: (подпись, значение).
        graph = self.force_graph
        suffix = self.current_force_suffix()

        def fmt(value):
            if value is None:
                return "—"
            return f"{self.convert_force_value(value):.2f} {suffix}"

        return [
            ("МИН", fmt(graph.cycle_min)),
            ("МАКС", fmt(graph.cycle_max)),
            ("СРЕДНЕЕ", fmt(graph.cycle_mid)),
            ("АМПЛИТУДА", fmt(graph.cycle_amplitude)),
            ("ЦИКЛЫ", str(graph.cycle_count)),
            ("ЧАСТОТА", self.measurement_freq_label.text()),
        ]

    def build_recent_table_rows(self):
        # Поцикловые строки за последние 30 с для отчёта
        # (значения переведены в единицы отображения).
        rows = []

        for row in self.session_recorder.recent_rows(30.0):
            rows.append((
                f"{row.get('elapsed_s', 0.0):.3f}",
                row.get("n", ""),
                f"{self.convert_force_value(row.get('max_n', 0.0)):.2f}",
                f"{self.convert_force_value(row.get('min_n', 0.0)):.2f}",
                f"{self.convert_force_value(row.get('mid_n', 0.0)):.2f}",
                f"{self.convert_force_value(row.get('amp_n', 0.0)):.2f}",
            ))

        return rows

    def build_graph_table_rows(self):
        # Данные графика (кадры) за последние 60 секунд для отчёта:
        # (время от старта, raw ADC, фильтрованный, сила в единицах
        # отображения).
        suffix = self.current_force_suffix()

        rows = []
        for elapsed, raw, filtered, force_n in (
                self.force_graph.get_recent_frames(60.0)
        ):
            rows.append((
                f"{elapsed:.3f}",
                f"{raw:.0f}",
                f"{filtered:.0f}",
                f"{self.convert_force_value(force_n):.3f} {suffix}",
            ))

        return rows

    def _ensure_session(self):
        # Есть активная сессия или данные о ней?
        if self.session_recorder.session_dir is None:
            QMessageBox.warning(
                self,
                "Экспорт",
                "Нет данных сессии. Запустите измерение (START).",
            )
            return False
        return True

    def export_session_pdf(self):
        if not self._ensure_session():
            return

        default_path = str(
            self.session_recorder.session_dir / "report.pdf"
        )

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить отчёт PDF",
            default_path,
            "PDF (*.pdf)",
        )

        if not path:
            return

        graph_pixmap = self.force_graph.grab()

        try:
            self.session_recorder.export_pdf(
                path,
                graph_pixmap,
                self.build_stats_rows(),
                self.build_recent_table_rows(),
                specimen_name=self.specimen_name_edit.text(),
                session_dir=self.session_recorder.session_dir,
                raw_rows=self.build_graph_table_rows(),
            )

            QMessageBox.information(
                self, "Экспорт PDF", f"Отчёт сохранён:\n{path}"
            )

        except Exception as e:
            logger.error(f"Экспорт PDF ERROR: {e}")
            QMessageBox.critical(
                self, "Экспорт PDF", f"Ошибка экспорта: {e}"
            )

    def export_session_csv(self):
        if not self._ensure_session():
            return

        default_path = str(
            self.session_recorder.session_dir / "report.csv"
        )

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить отчёт CSV",
            default_path,
            "CSV (*.csv)",
        )

        if not path:
            return

        try:
            self.session_recorder.export_csv_tables(
                path,
                self.build_stats_rows(),
                self.build_recent_table_rows(),
                specimen_name=self.specimen_name_edit.text(),
                session_dir=self.session_recorder.session_dir,
                raw_rows=self.build_graph_table_rows(),
            )

            QMessageBox.information(
                self, "Экспорт CSV", f"Отчёт сохранён:\n{path}"
            )

        except Exception as e:
            logger.error(f"Экспорт CSV ERROR: {e}")
            QMessageBox.critical(
                self, "Экспорт CSV", f"Ошибка экспорта: {e}"
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

        # Ограничение цели верхней границей хода (Настройки).
        limit_mm = self.get_traverse_max_mm()

        if target_mm > limit_mm:
            self.append_log(
                f"ОГРАНИЧЕНИЕ: точка {target_mm:.3f} мм выше "
                f"верхней границы {limit_mm:.1f} мм — "
                f"цель ограничена"
            )
            logger.info(
                f"TRAVERSE: цель MOVE {target_mm:.3f} мм ограничена "
                f"верхней границей {limit_mm:.1f} мм"
            )
            target_mm = limit_mm

        command = (
            f"MOVE_0_{target_mm:.6f}_{speed_mm_s:.6f}_YYY"
        )

        self.send_command(command)

    # ========================================================
    # FORCE MAINTAINING
    # ========================================================

    # Полоса допуска с гистерезисом: ловим при |err| <= CATCH,
    # отпускаем только при |err| > RELEASE (вдвое шире) — иначе
    # регулятор дребезжит на границе: поймал/отпустил каждый такт.
    MAINTAIN_CATCH_TOL = 0.005        # Н, захват цели (практически 1:1)
    MAINTAIN_RELEASE_TOL = 0.010      # Н, выход из допуска
    MAINTAIN_PERIOD_MS = 200          # такт регулятора
    # Окно усреднения силы для принятия решения, с.
    MAINTAIN_WINDOW_S = 4.0
    # Авто-скорость: экспоненциальная зависимость от ошибки.
    # v = v_max * (1 - exp(-|err| / tau)), с нижней планкой v_min.
    MAINTAIN_AUTO_SPEED_MAX = 5.0     # мм/с, насыщение вдали от цели
    MAINTAIN_AUTO_SPEED_MIN = 0.001   # мм/с, нижняя планка
    MAINTAIN_SPEED_TAU_N = 1.0        # Н, постоянная времени экспоненты
    # Ход за такт берётся с запасом, чтобы траверса не останавливалась
    # между тактами регулятора (движение непрерывное).
    MAINTAIN_STEP_FACTOR = 1.5
    # Подъём траверсы увеличивает силу; если на стенде наоборот —
    # поставить False (направление регулятора инвертируется).
    MAINTAIN_UP_INCREASES_FORCE = True

    # Порог начала оценки циклов: управляемая величина
    # должна подойти к цели на допуск (в единицах
    # отображения), иначе показания нестабильны.
    CYCLE_START_TOL_N = 0.1     # Н
    CYCLE_START_TOL_MPA = 0.1   # МПа

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

            target_n_actual = self.convert_display_to_n(
                target_n
            )

            if target_n_actual is None:
                self.append_log(
                    "ОШИБКА: для МПа задайте размеры образца "
                    "(блок «Параметры образца»)"
                )
                return

            self.maintain_target_n = target_n_actual
            self.maintain_speed_mm_s = speed_mm_s
            self.maintain_active = True
            self.maintain_last_direction = 0
            self.maintain_caught = False
            self.maintain_window = []

            self.maintain_button.setText(
                "ОСТАНОВИТЬ ПОДДЕРЖИВАНИЕ"
            )
            self.maintain_timer.start(
                self.MAINTAIN_PERIOD_MS
            )
            logger.info(f"MAINTAIN: старт, цель {target_n_actual:.3f} N, скорость {speed_mm_s:.3f} mm/s")

            # Блок «Положение траверсы» блокируется:
            # регулятор сам управляет траверсой.
            self.set_traverse_block_enabled(False)

            self.append_log(
                f"MAINTAIN: старт, цель "
                f"{target_n:.3f} {self.maintain_units}"
                f" = {target_n_actual:.3f} N, "
                f"скорость {speed_mm_s:.3f} mm/s"
            )

        else:

            self.maintain_active = False
            self.maintain_caught = False
            self.maintain_window = []
            self.maintain_timer.stop()
            logger.info("MAINTAIN: остановлено")

            self.maintain_button.setText(
                "НАЧАТЬ ПОДДЕРЖИВАТЬ"
            )

            # Блок «Положение траверсы» возвращается пользователю.
            self.set_traverse_block_enabled(True)

            # Остановка движения: команда в текущую позицию.
            if self.last_current_mm is not None:
                self.send_command(
                    f"MOVE_0_{self.last_current_mm:.6f}_"
                    f"{self.maintain_speed_mm_s:.6f}_YYY"
                )

            self.append_log(
                "MAINTAIN: остановлено"
            )

    def set_traverse_block_enabled(self, enabled):

        # Read-only для пользователя, пока работает поддержание
        # силы: регулятор сам управляет траверсой.
        self.traverse_target_edit.setReadOnly(not enabled)
        self.traverse_speed_edit.setReadOnly(not enabled)
        self.traverse_move_button.setEnabled(enabled)

        if enabled:
            self.maintain_group.setTitle(
                "УПРАВЛЕНИЕ"
            )
        else:
            self.maintain_group.setTitle(
                "УПРАВЛЕНИЕ (траверса управляется регулятором силы)"
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

        logger.debug(f"RCV MAINTAIN: force={force:.3f}N, window_len={len(self.maintain_window)}")

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

        # ----------------------------------------------------
        # Управляемая величина:
        # - есть циклы: среднее между MIN и MAX последнего
        #   завершённого цикла (то же, что карточка «СРЕДНЕЕ»);
        # - нет циклов: среднее мгновенной силы за окно 1 с.
        # ----------------------------------------------------

        graph = self.force_graph

        # ----------------------------------------------------
        # Защита от заморозки управляемого среднего: если
        # циклы перестали завершаться (регулятор движется
        # быстрее осцилляции и сила не пересекает старый mid),
        # значение (MIN+MAX)/2 устаревает и регулятор слепнет.
        # Нет свежих циклов дольше 3 с — возвращаемся к
        # среднему мгновенной силы за окно 1 с.
        # ----------------------------------------------------

        if graph.cycle_count != getattr(
                self, "maintain_ctrl_count", None
        ):
            self.maintain_ctrl_count = graph.cycle_count
            self.maintain_ctrl_changed_t = now

        cycles_stale = (
            now
            - getattr(
                self,
                "maintain_ctrl_changed_t",
                now,
            )
            > 3.0
        )

        if (
                self.measurement_running
                and not cycles_stale
                and graph.cycle_min is not None
                and graph.cycle_max is not None
        ):
            control_value = (
                    graph.cycle_min
                    + graph.cycle_max
            ) / 2.0
        else:
            # Среднее за последние 1 с данных.
            cutoff = now - 1.0

            recent = [
                f for t, f in self.maintain_window
                if t >= cutoff
            ]

            control_value = (
                    sum(recent) / len(recent)
                    if recent
                    else window_mean
            )

        error = (
                self.maintain_target_n
                - control_value
        )

        logger.debug(f"RCV MAINTAIN: target={self.maintain_target_n:.3f}N, control_value={control_value:.3f}N, error={error:.3f}N")

        if not self.MAINTAIN_UP_INCREASES_FORCE:
            error = -error

        # ----------------------------------------------------
        # Цель поймана: прекратить подстройки. Возобновить —
        # только когда среднее за окно выйдет за release-границу
        # (гистерезис, иначе дребезг на границе допуска).
        # ----------------------------------------------------

        catch_tol = self.MAINTAIN_CATCH_TOL
        release_tol = self.MAINTAIN_RELEASE_TOL

        if self.maintain_caught:

            if abs(error) <= release_tol:
                return

            self.maintain_caught = False
            self.maintain_last_direction = 0
            logger.debug(f"RCV MAINTAIN: выход из допуска, error={error:.3f} N")
            self.append_log(
                f"MAINTAIN: выход из допуска "
                f"(ср. {control_value:.3f} N), "
                f"подстройки возобновлены"
            )

        else:

            if abs(error) <= catch_tol:

                self.maintain_caught = True
                self.maintain_last_direction = 0
                logger.debug(f"SND MAINTAIN: цель поймана (MOVE в текущую позицию), error={error:.3f} N")

                # Остановка движения: команда в текущую позицию.
                self.send_command(
                    f"MOVE_0_{current_mm:.6f}_"
                    f"{self.maintain_speed_mm_s:.6f}_YYY"
                )

                self.append_log(
                    f"MAINTAIN: цель поймана "
                    f"(ср. {control_value:.3f} N), "
                    f"подстройки остановлены"
                )

                return

        # ----------------------------------------------------
        # Непрерывная подстройка: каждый такт регулятор
        # выдаёт MOVE к позиции на полтакта впереди по
        # текущей скорости. Скорость — экспоненциальная
        # функция ошибки: вдали от цели — быстро, у цели —
        # медленно (нижняя планка v_min), скачков нет.
        # ----------------------------------------------------

        period_s = self.MAINTAIN_PERIOD_MS / 1000.0

        if self.maintain_auto_speed_check.isChecked():
            # Скорость — от той же ошибки, по которой
            # принимается решение (среднее мин/макс при
            # осцилляции), иначе скорость зануляется раньше,
            # чем управляемое среднее доходит до цели.
            speed_mm_s = (
                    self.MAINTAIN_AUTO_SPEED_MAX
                    * (
                            1.0
                            - math.exp(
                                -abs(error)
                                / self.MAINTAIN_SPEED_TAU_N
                            )
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

        # Диагностика зависания: команда в ту же сторону,
        # а управляемая величина не меняется — вероятно,
        # траверса упёрлась в предел хода.
        if (
                direction == self.maintain_last_direction
                and abs(
                    control_value
                    - getattr(
                        self,
                        "maintain_last_control",
                        control_value,
                    )
                )
                < 1e-9
        ):
            stall_since = getattr(
                self,
                "maintain_stall_since",
                None,
            )

            if stall_since is None:
                self.maintain_stall_since = now
            elif now - stall_since > 5.0:
                self.maintain_stall_since = now
                self.append_log(
                    f"MAINTAIN: ПОДОЗРА НА ЗАЛИПАНИЕ — "
                    f"управляемое значение не меняется "
                    f"({control_value:.3f} N), возможно, траверса "
                    f"упёрлась в предел хода"
                )
        else:
            self.maintain_stall_since = None

        self.maintain_last_control = control_value

        # Ход за такт: скорость × период × запас,
        # чтобы траверса не останавливалась между тактами.
        step_mm = (
                speed_mm_s
                * period_s
                * self.MAINTAIN_STEP_FACTOR
        )

        target_mm = (
                current_mm
                + direction * step_mm
        )

        # Цель регулятора не выше верхней границы хода (Настройки).
        target_mm = min(
            target_mm,
            self.get_traverse_max_mm(),
        )

        self.send_command(
            f"MOVE_0_{target_mm:.6f}_"
            f"{speed_mm_s:.6f}_YYY"
        )

        # В лог — только смена направления движения,
        # иначе лог захлебнётся.
        if direction != self.maintain_last_direction:
            logger.debug(f"SND MAINTAIN: MOVE {('вверх' if direction > 0 else 'вниз')}, speed={speed_mm_s:.3f}mm/s, step={step_mm:.3f}mm, mean={window_mean:.3f}N")
            self.maintain_last_direction = direction
            self.append_log(
                f"MAINTAIN: ср. {window_mean:.3f} N, "
                f"движение {'вверх' if direction > 0 else 'вниз'} "
                f"(шаг {step_mm:.3f} мм, {speed_mm_s:.3f} мм/с)"
            )


# ============================================================
# APPLICATION
# ============================================================
