import sys
import time
import queue
import struct

import numpy as np
import serial
import serial.tools.list_ports

from scipy.signal import butter, sosfilt, sosfilt_zi, savgol_filter

from PyQt5.QtCore import (
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    QObject,
)

from PyQt5.QtGui import QColor

from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QCheckBox,
    QSlider,
    QGroupBox,
    QColorDialog,
    QSpinBox,
    QDoubleSpinBox,
    QMessageBox,
)

import pyqtgraph as pg


# ============================================================
# НАСТРОЙКИ
# ============================================================

SAMPLE_RATE = 330.0

SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.0

FRAME_SIZE = 10
FRAME_FORMAT = "<BiiB"

START_MARKER = 0xAA
END_MARKER = 0xBB


# Начальная ёмкость хранилища.
#
# Это НЕ максимальное количество точек.
# При заполнении массив автоматически увеличивается.
#
INITIAL_STORAGE_POINTS = 200000


# ============================================================
# DISPLAY
# ============================================================

# Максимальное количество точек, которое физически
# передаём PyQtGraph для одного графика.
#
# Это НЕ ограничивает сохранение данных.

MAX_DISPLAY_POINTS = 4000


# ============================================================
# ВИДИМОЕ ОКНО
# ============================================================

# Максимальная ширина отображаемого окна = 1 секунда.
#
# При SAMPLE_RATE = 330 Hz:
#
# 1 секунда = 330 точек.
#
# Вся история продолжает храниться полностью.

MAX_WINDOW_SECONDS = 1.0

MAX_WINDOW_POINTS = int(
    MAX_WINDOW_SECONDS * SAMPLE_RATE
)


# ============================================================
# ФИЛЬТРАЦИЯ
# ============================================================

MEDIAN_SIZE = 5

BUTTER_ORDER = 4
BUTTER_CUTOFF = 40.0

SG_WINDOW = 21
SG_POLYORDER = 3


# ============================================================
# ЦВЕТА
# ============================================================

DEFAULT_RAW_COLOR = "#FFD400"
DEFAULT_FILTERED_COLOR = "#00F6FF"
DEFAULT_SMOOTH_COLOR = "#FF6B6B"

DEFAULT_RAW_WIDTH = 1
DEFAULT_FILTERED_WIDTH = 2
DEFAULT_SMOOTH_WIDTH = 2


# ============================================================
# НАЧАЛЬНАЯ ШИРИНА ОКНА
# ============================================================

# По умолчанию показываем 0.5 секунды.

DEFAULT_WINDOW_SECONDS = 0.5

DEFAULT_WINDOW_POINTS = int(
    DEFAULT_WINDOW_SECONDS * SAMPLE_RATE
)


# ============================================================
# АВТОМАТИЧЕСКИЙ Y-МАСШТАБ
# ============================================================

# Фиксированный запас сверху и снизу.
#
# Например:
#
# Ymin = 1000
# Ymax = 2000
#
# диапазон = 1000
#
# запас = 100
#
# итог:
#
# Y = 900 ... 2100

Y_AXIS_MARGIN = 0.10


# ============================================================
# ОБНОВЛЕНИЕ GUI
# ============================================================

GUI_UPDATE_MS = 50

DIAGNOSTICS_INTERVAL = 0.25


# ============================================================
# ТЁМНЫЙ СТИЛЬ
# ============================================================

def apply_dark_style(app):

    app.setStyle("Fusion")

    app.setStyleSheet("""
        QWidget {
            background-color: #0b1017;
            color: #e8eef5;
            font-size: 10pt;
        }

        QMainWindow {
            background-color: #070b10;
        }

        QGroupBox {
            border: 1px solid #26313d;
            border-radius: 7px;
            margin-top: 10px;
            padding-top: 8px;
            background-color: #0d131b;
            font-weight: bold;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
            color: #b8c7d9;
        }

        QPushButton {
            background-color: #18232f;
            border: 1px solid #344556;
            border-radius: 5px;
            padding: 6px 12px;
            color: #f2f6fa;
        }

        QPushButton:hover {
            background-color: #223140;
            border-color: #4b657d;
        }

        QPushButton:pressed {
            background-color: #101922;
        }

        QPushButton:disabled {
            color: #65717d;
            background-color: #111820;
        }

        QComboBox,
        QSpinBox,
        QDoubleSpinBox {
            background-color: #111a23;
            border: 1px solid #344556;
            border-radius: 4px;
            padding: 4px 7px;
            color: #e8eef5;
        }

        QComboBox QAbstractItemView {
            background-color: #111a23;
            color: #e8eef5;
            selection-background-color: #27435b;
        }

        QSlider::groove:horizontal {
            height: 5px;
            background: #263441;
            border-radius: 2px;
        }

        QSlider::handle:horizontal {
            width: 15px;
            margin: -5px 0;
            border-radius: 7px;
            background: #00d9ff;
        }

        QCheckBox {
            spacing: 7px;
        }

        QCheckBox::indicator {
            width: 15px;
            height: 15px;
        }

        QCheckBox::indicator:unchecked {
            background-color: #111820;
            border: 1px solid #536474;
            border-radius: 3px;
        }

        QCheckBox::indicator:checked {
            background-color: #00a8cc;
            border: 1px solid #00dfff;
            border-radius: 3px;
        }

        QLabel {
            color: #c8d3df;
        }
    """)


# ============================================================
# СЛАЙДЕР С ТОЧНОЙ НАСТРОЙКОЙ КОЛЕСОМ
# ============================================================

class FineSlider(QSlider):

    """
    Обычный QSlider, но колесо мыши изменяет значение
    ровно на 1 шаг.

    Для ширины окна:
        колесо вверх   -> +1 точка
        колесо вниз    -> -1 точка
    """

    def wheelEvent(self, event):

        delta = event.angleDelta().y()

        if delta > 0:

            self.setValue(
                min(
                    self.maximum(),
                    self.value() + 1
                )
            )

        elif delta < 0:

            self.setValue(
                max(
                    self.minimum(),
                    self.value() - 1
                )
            )

        event.accept()


# ============================================================
# ПОТОК РАБОТЫ С SERIAL
# ============================================================

class SerialWorker(QObject):

    data_ready = pyqtSignal(object, object)
    status = pyqtSignal(str)

    connected = pyqtSignal()
    disconnected = pyqtSignal()

    diagnostics = pyqtSignal(object)

    def __init__(self):

        super().__init__()

        self.command_queue = queue.Queue()

        self.running = True

        self.serial_port = None
        self.port_name = None

        self.streaming = False

        self.rx_buffer = bytearray()

        self.sample_index = 0

        self.frames_received = 0
        self.bytes_received = 0
        self.bad_packets = 0

        self.last_diagnostics_time = time.monotonic()

    # ========================================================
    # КОМАНДЫ
    # ========================================================

    def request_connect(self, port_name):

        self.command_queue.put(
            ("connect", port_name)
        )

    def request_disconnect(self):

        self.command_queue.put(
            ("disconnect", None)
        )

    def request_start(self):

        self.command_queue.put(
            ("start", None)
        )

    def request_stop(self):

        self.command_queue.put(
            ("stop", None)
        )

    def request_shutdown(self):

        self.command_queue.put(
            ("shutdown", None)
        )

    # ========================================================
    # ОСНОВНОЙ ЦИКЛ
    # ========================================================

    def run(self):

        self.status.emit(
            "Поток запущен"
        )

        while self.running:

            self.process_commands()

            if not self.running:
                break

            if (
                self.serial_port is not None
                and self.serial_port.is_open
            ):

                try:

                    data = self.serial_port.read(
                        4096
                    )

                    if data:

                        self.bytes_received += len(data)

                        self.rx_buffer.extend(
                            data
                        )

                        self.parse_packets()

                except Exception as exc:

                    self.status.emit(
                        f"Ошибка Serial: {exc}"
                    )

                    self.close_serial()

            self.emit_diagnostics_if_needed()

            time.sleep(0.001)

        self.close_serial()

        self.status.emit(
            "Поток остановлен"
        )

    # ========================================================
    # ОБРАБОТКА КОМАНД
    # ========================================================

    def process_commands(self):

        while True:

            try:

                command, argument = \
                    self.command_queue.get_nowait()

            except queue.Empty:

                break

            if command == "connect":

                self.open_serial(argument)

            elif command == "disconnect":

                self.close_serial()

            elif command == "start":

                self.start_measurement()

            elif command == "stop":

                self.stop_measurement()

            elif command == "shutdown":

                self.running = False

    # ========================================================
    # ПОДКЛЮЧЕНИЕ
    # ========================================================

    def open_serial(self, port_name):

        self.close_serial()

        try:

            self.serial_port = serial.Serial(
                port=port_name,
                baudrate=SERIAL_BAUD,
                timeout=SERIAL_TIMEOUT,
            )

            self.port_name = port_name

            self.rx_buffer.clear()

            self.status.emit(
                f"Подключено: {port_name}"
            )

            self.connected.emit()

        except Exception as exc:

            self.serial_port = None
            self.port_name = None

            self.status.emit(
                f"Ошибка подключения: {exc}"
            )

    def close_serial(self):

        if self.serial_port is not None:

            try:

                if self.serial_port.is_open:

                    self.serial_port.close()

            except Exception:

                pass

        was_connected = (
            self.port_name is not None
        )

        self.serial_port = None
        self.port_name = None

        self.streaming = False

        if was_connected:

            self.disconnected.emit()

    # ========================================================
    # СТАРТ / СТОП
    # ========================================================

    def start_measurement(self):

        self.streaming = True

        self.sample_index = 0

        self.rx_buffer.clear()

        self.frames_received = 0
        self.bytes_received = 0
        self.bad_packets = 0

        self.status.emit(
            "Измерение запущено"
        )

    def stop_measurement(self):

        self.streaming = False

        self.status.emit(
            "Измерение остановлено"
        )

    # ========================================================
    # РАЗБОР ПАКЕТОВ
    # ========================================================

    def parse_packets(self):

        while len(self.rx_buffer) >= FRAME_SIZE:

            try:

                start_index = \
                    self.rx_buffer.index(
                        START_MARKER
                    )

            except ValueError:

                self.bad_packets += \
                    len(self.rx_buffer)

                self.rx_buffer.clear()

                return

            if start_index > 0:

                self.bad_packets += \
                    start_index

                del self.rx_buffer[
                    :start_index
                ]

            if len(self.rx_buffer) < FRAME_SIZE:

                return

            frame = bytes(
                self.rx_buffer[
                    :FRAME_SIZE
                ]
            )

            try:

                (
                    start_marker,
                    force_raw,
                    force_smooth,
                    end_marker,
                ) = struct.unpack(
                    FRAME_FORMAT,
                    frame
                )

            except Exception:

                self.bad_packets += 1

                del self.rx_buffer[0]

                continue

            if start_marker != START_MARKER:

                self.bad_packets += 1

                del self.rx_buffer[0]

                continue

            if end_marker != END_MARKER:

                self.bad_packets += 1

                del self.rx_buffer[0]

                continue

            del self.rx_buffer[
                :FRAME_SIZE
            ]

            if not self.streaming:

                continue

            t = (
                self.sample_index
                / SAMPLE_RATE
            )

            self.sample_index += 1

            self.frames_received += 1

            # ВАЖНО:
            #
            # force_raw   -> RAW
            # force_smooth -> SMOOTH
            #
            # Второе значение BiiB НЕ игнорируется.

            self.data_ready.emit(
                t,
                (
                    int(force_raw),
                    int(force_smooth),
                )
            )

    # ========================================================
    # ДИАГНОСТИКА
    # ========================================================

    def emit_diagnostics_if_needed(self):

        now = time.monotonic()

        if (
            now - self.last_diagnostics_time
            >= DIAGNOSTICS_INTERVAL
        ):

            self.last_diagnostics_time = now

            self.diagnostics.emit({

                "frames":
                    self.frames_received,

                "bytes":
                    self.bytes_received,

                "bad":
                    self.bad_packets,

                "streaming":
                    self.streaming,

                "connected":
                    self.serial_port is not None,

            })


# ============================================================
# ГЛАВНОЕ ОКНО
# ============================================================

class MainWindow(QMainWindow):

    def __init__(self):

        super().__init__()

        self.setWindowTitle(
            "STM32 ADS1220 — Осциллограф"
        )

        self.resize(
            1500,
            900
        )

        # ====================================================
        # ДАННЫЕ
        # ====================================================

        self.storage_capacity = \
            INITIAL_STORAGE_POINTS

        self.time_data = np.empty(
            self.storage_capacity,
            dtype=np.float64
        )

        self.raw_data = np.empty(
            self.storage_capacity,
            dtype=np.float64
        )

        # ====================================================
        # SMOOTH
        #
        # Второе значение int32 из BiiB.
        # Это данные непосредственно от STM32.
        # ====================================================

        self.smooth_data = np.empty(
            self.storage_capacity,
            dtype=np.float64
        )

        self.filtered_time_data = np.empty(
            self.storage_capacity,
            dtype=np.float64
        )

        self.filtered_data = np.empty(
            self.storage_capacity,
            dtype=np.float64
        )

        self.data_count = 0
        self.filtered_count = 0

        # ====================================================
        # ФИЛЬТРЫ
        # ====================================================

        self.median_buffer = []

        self.butter_sos = butter(
            BUTTER_ORDER,
            BUTTER_CUTOFF,
            fs=SAMPLE_RATE,
            output="sos",
        )

        self.butter_state = sosfilt_zi(
            self.butter_sos
        )

        self.sg_buffer = []

        self.sg_center_delay = \
            SG_WINDOW // 2

        # ====================================================
        # ОКНО ПРОСМОТРА
        # ====================================================

        self.window_width = \
            DEFAULT_WINDOW_POINTS

        self.position = 0

        self.live_follow = True

        self.measurement_active = False

        # ====================================================
        # СОСТОЯНИЕ ОТРИСОВКИ
        # ====================================================

        self.data_changed = False
        self.view_changed = True

        self.last_plot_left = -1
        self.last_plot_right = -1

        self.last_plot_live = None
        self.last_slider_count = -1

        # ====================================================
        # НАСТРОЙКИ КРИВЫХ
        # ====================================================

        self.raw_color = \
            DEFAULT_RAW_COLOR

        self.filtered_color = \
            DEFAULT_FILTERED_COLOR

        self.smooth_color = \
            DEFAULT_SMOOTH_COLOR

        self.raw_width = \
            DEFAULT_RAW_WIDTH

        self.filtered_width = \
            DEFAULT_FILTERED_WIDTH

        self.smooth_width = \
            DEFAULT_SMOOTH_WIDTH

        self.raw_style = Qt.SolidLine
        self.filtered_style = Qt.SolidLine
        self.smooth_style = Qt.SolidLine

        # ====================================================
        # SERIAL WORKER
        # ====================================================

        self.worker_thread = QThread(self)

        self.worker = SerialWorker()

        self.worker.moveToThread(
            self.worker_thread
        )

        self.worker_thread.started.connect(
            self.worker.run
        )

        self.worker.data_ready.connect(
            self.on_data_ready
        )

        self.worker.status.connect(
            self.on_worker_status
        )

        self.worker.connected.connect(
            self.on_connected
        )

        self.worker.disconnected.connect(
            self.on_disconnected
        )

        self.worker.diagnostics.connect(
            self.on_diagnostics
        )

        self.worker_thread.start()

        # ====================================================
        # UI
        # ====================================================

        self.build_ui()

        # ====================================================
        # ТАЙМЕР
        # ====================================================

        self.update_timer = QTimer(self)

        self.update_timer.timeout.connect(
            self.update_plot
        )

        self.update_timer.start(
            GUI_UPDATE_MS
        )

    # ========================================================
    # АВТОМАТИЧЕСКОЕ УВЕЛИЧЕНИЕ ХРАНИЛИЩА
    # ========================================================

    def ensure_storage_capacity(
        self,
        required_count
    ):

        if required_count <= \
                self.storage_capacity:

            return

        new_capacity = \
            self.storage_capacity

        while new_capacity < required_count:

            new_capacity *= 2

        new_time = np.empty(
            new_capacity,
            dtype=np.float64
        )

        new_raw = np.empty(
            new_capacity,
            dtype=np.float64
        )

        new_smooth = np.empty(
            new_capacity,
            dtype=np.float64
        )

        new_filtered_time = np.empty(
            new_capacity,
            dtype=np.float64
        )

        new_filtered = np.empty(
            new_capacity,
            dtype=np.float64
        )

        if self.data_count > 0:

            new_time[
                :self.data_count
            ] = self.time_data[
                :self.data_count
            ]

            new_raw[
                :self.data_count
            ] = self.raw_data[
                :self.data_count
            ]

            new_smooth[
                :self.data_count
            ] = self.smooth_data[
                :self.data_count
            ]

        if self.filtered_count > 0:

            new_filtered_time[
                :self.filtered_count
            ] = self.filtered_time_data[
                :self.filtered_count
            ]

            new_filtered[
                :self.filtered_count
            ] = self.filtered_data[
                :self.filtered_count
            ]

        self.time_data = new_time
        self.raw_data = new_raw
        self.smooth_data = new_smooth

        self.filtered_time_data = \
            new_filtered_time

        self.filtered_data = \
            new_filtered

        self.storage_capacity = \
            new_capacity

    # ========================================================
    # СОЗДАНИЕ UI
    # ========================================================

    def build_ui(self):

        central = QWidget()

        self.setCentralWidget(
            central
        )

        main_layout = QVBoxLayout(
            central
        )

        main_layout.setContentsMargins(
            8,
            8,
            8,
            8
        )

        main_layout.setSpacing(
            6
        )

        # ====================================================
        # ВЕРХНЯЯ ПАНЕЛЬ
        # ====================================================

        top = QHBoxLayout()

        top.addWidget(
            QLabel("Порт:")
        )

        self.port_combo = QComboBox()

        self.port_combo.setMinimumWidth(
            170
        )

        top.addWidget(
            self.port_combo
        )

        self.refresh_button = \
            QPushButton("ОБНОВИТЬ")

        self.refresh_button.clicked.connect(
            self.refresh_ports
        )

        top.addWidget(
            self.refresh_button
        )

        self.connect_button = \
            QPushButton("ПОДКЛЮЧИТЬ")

        self.connect_button.clicked.connect(
            self.toggle_connection
        )

        top.addWidget(
            self.connect_button
        )

        self.start_button = \
            QPushButton("СТАРТ")

        self.start_button.setMinimumWidth(
            90
        )

        self.start_button.clicked.connect(
            self.start_measurement
        )

        top.addWidget(
            self.start_button
        )

        self.stop_button = \
            QPushButton("СТОП")

        self.stop_button.setMinimumWidth(
            90
        )

        self.stop_button.clicked.connect(
            self.stop_measurement
        )

        top.addWidget(
            self.stop_button
        )

        self.live_button = \
            QPushButton("ЖИВОЙ РЕЖИМ")

        self.live_button.setCheckable(
            True
        )

        self.live_button.setChecked(
            True
        )

        self.live_button.clicked.connect(
            self.toggle_live
        )

        top.addWidget(
            self.live_button
        )

        top.addStretch()

        self.status_label = QLabel(
            "Отключено"
        )

        self.status_label.setStyleSheet(
            "color:#8fa2b5; "
            "font-weight:bold;"
        )

        top.addWidget(
            self.status_label
        )

        main_layout.addLayout(
            top
        )

        # ====================================================
        # ОБЛАСТЬ ГРАФИКА
        #
        # СЛЕВА:
        #     Параметры испытания
        #
        # СПРАВА:
        #     График
        # ====================================================

        graph_area = QHBoxLayout()

        graph_area.setContentsMargins(
            0,
            0,
            0,
            0
        )

        graph_area.setSpacing(
            6
        )

        # ====================================================
        # ПАНЕЛЬ ПАРАМЕТРОВ
        # ====================================================

        parameters_group = QGroupBox(
            "ПАРАМЕТРЫ ИСПЫТАНИЯ"
        )

        parameters_group.setMinimumWidth(
            220
        )

        parameters_group.setMaximumWidth(
            260
        )

        parameters_layout = QVBoxLayout(
            parameters_group
        )

        parameters_layout.setContentsMargins(
            10,
            12,
            10,
            10
        )

        parameters_layout.setSpacing(
            8
        )

        # ----------------------------------------------------
        # ЧАСТОТА
        # ----------------------------------------------------

        frequency_label = QLabel(
            "Частота колебаний, Hz"
        )

        parameters_layout.addWidget(
            frequency_label
        )

        self.frequency_input = \
            QDoubleSpinBox()

        self.frequency_input.setRange(
            0.0,
            1000.0
        )

        self.frequency_input.setDecimals(
            3
        )

        self.frequency_input.setSingleStep(
            0.1
        )

        self.frequency_input.setValue(
            0.0
        )

        self.frequency_input.setSuffix(
            " Hz"
        )

        parameters_layout.addWidget(
            self.frequency_input
        )

        # ----------------------------------------------------
        # АМПЛИТУДА
        # ----------------------------------------------------

        amplitude_label = QLabel(
            "Амплитуда колебаний, mm"
        )

        parameters_layout.addWidget(
            amplitude_label
        )

        self.amplitude_input = \
            QDoubleSpinBox()

        self.amplitude_input.setRange(
            0.0,
            1000.0
        )

        self.amplitude_input.setDecimals(
            3
        )

        self.amplitude_input.setSingleStep(
            0.1
        )

        self.amplitude_input.setValue(
            0.0
        )

        self.amplitude_input.setSuffix(
            " mm"
        )

        parameters_layout.addWidget(
            self.amplitude_input
        )

        # ----------------------------------------------------
        # ТРЕБУЕМОЕ УСИЛИЕ
        # ----------------------------------------------------

        force_label = QLabel(
            "Поддерживаемое усилие, N"
        )

        parameters_layout.addWidget(
            force_label
        )

        self.target_force_input = \
            QDoubleSpinBox()

        self.target_force_input.setRange(
            0.0,
            10000.0
        )

        self.target_force_input.setDecimals(
            3
        )

        self.target_force_input.setSingleStep(
            0.1
        )

        self.target_force_input.setValue(
            0.0
        )

        self.target_force_input.setSuffix(
            " N"
        )

        parameters_layout.addWidget(
            self.target_force_input
        )

        parameters_layout.addStretch()

        graph_area.addWidget(
            parameters_group
        )

        # ====================================================
        # ГРАФИК
        # ====================================================

        self.plot = pg.PlotWidget()

        self.plot.setBackground(
            "#070b10"
        )

        self.plot.showGrid(
            x=True,
            y=True,
            alpha=0.22,
        )

        self.plot.setLabel(
            "bottom",
            "Время",
            units="с",
        )

        self.plot.setLabel(
            "left",
            "Значение АЦП / сила",
        )

        self.plot.getAxis(
            "bottom"
        ).setPen(
            pg.mkPen("#718398")
        )

        self.plot.getAxis(
            "left"
        ).setPen(
            pg.mkPen("#718398")
        )

        self.plot.getAxis(
            "bottom"
        ).setTextPen(
            pg.mkPen("#aebdca")
        )

        self.plot.getAxis(
            "left"
        ).setTextPen(
            pg.mkPen("#aebdca")
        )

        self.plot.setMouseEnabled(
            x=True,
            y=True,
        )

        self.plot.setMenuEnabled(
            True
        )

        # ====================================================
        # ЛЕГЕНДА
        # ====================================================

        self.legend = \
            self.plot.addLegend(
                offset=(15, 15)
            )

        # ====================================================
        # RAW
        # ====================================================

        self.raw_curve = \
            self.plot.plot(
                [],
                [],
                name="RAW",
                pen=pg.mkPen(
                    self.raw_color,
                    width=self.raw_width,
                ),
            )

        self.raw_curve.setClipToView(
            True
        )

        self.raw_curve.setDownsampling(
            auto=True,
            method="peak",
            ds=1,
        )

        # ====================================================
        # FILTERED
        # ====================================================

        self.filtered_curve = \
            self.plot.plot(
                [],
                [],
                name="FILTERED",
                pen=pg.mkPen(
                    self.filtered_color,
                    width=self.filtered_width,
                ),
            )

        self.filtered_curve.setClipToView(
            True
        )

        self.filtered_curve.setDownsampling(
            auto=True,
            method="peak",
            ds=1,
        )

        # ====================================================
        # SMOOTH
        #
        # Второе значение force_smooth из BiiB.
        #
        # Это НЕ Python FILTERED.
        # Это значение непосредственно от STM32.
        # ====================================================

        self.smooth_curve = \
            self.plot.plot(
                [],
                [],
                name="SMOOTH",
                pen=pg.mkPen(
                    self.smooth_color,
                    width=self.smooth_width,
                ),
            )

        self.smooth_curve.setClipToView(
            True
        )

        self.smooth_curve.setDownsampling(
            auto=True,
            method="peak",
            ds=1,
        )

        graph_area.addWidget(
            self.plot,
            stretch=1,
        )

        # ====================================================
        # ДОБАВЛЯЕМ ОБЛАСТЬ ГРАФИКА В ОСНОВНОЙ LAYOUT
        # ====================================================

        main_layout.addLayout(
            graph_area,
            stretch=1,
        )

        # ====================================================
        # ПОЗИЦИЯ
        # ====================================================

        position_group = QGroupBox(
            "ПОЗИЦИЯ — перемещение по записи"
        )

        position_layout = QHBoxLayout(
            position_group
        )

        position_layout.addWidget(
            QLabel("← ИСТОРИЯ")
        )

        self.position_slider = \
            QSlider(Qt.Horizontal)

        self.position_slider.setMinimum(
            0
        )

        self.position_slider.setMaximum(
            0
        )

        self.position_slider.valueChanged.connect(
            self.on_position_slider
        )

        position_layout.addWidget(
            self.position_slider,
            stretch=1,
        )

        position_layout.addWidget(
            QLabel("ТЕКУЩЕЕ →")
        )

        main_layout.addWidget(
            position_group
        )

        # ====================================================
        # ШИРИНА
        # ====================================================

        width_group = QGroupBox(
            "ШИРИНА / МАСШТАБ — видимое окно"
        )

        width_layout = QHBoxLayout(
            width_group
        )

        width_layout.addWidget(
            QLabel("УЗКО")
        )

        self.width_slider = \
            FineSlider(Qt.Horizontal)

        self.width_slider.setMinimum(
            1
        )

        self.width_slider.setMaximum(
            MAX_WINDOW_POINTS
        )

        self.width_slider.setValue(
            min(
                DEFAULT_WINDOW_POINTS,
                MAX_WINDOW_POINTS
            )
        )

        self.width_slider.valueChanged.connect(
            self.on_width_slider
        )

        width_layout.addWidget(
            self.width_slider,
            stretch=1,
        )

        width_layout.addWidget(
            QLabel("ШИРОКО")
        )

        self.width_value_label = QLabel()

        self.width_value_label.setMinimumWidth(
            100
        )

        width_layout.addWidget(
            self.width_value_label
        )

        main_layout.addWidget(
            width_group
        )

        # ====================================================
        # КРИВЫЕ
        # ====================================================

        curves_group = QGroupBox(
            "НАСТРОЙКИ КРИВЫХ"
        )

        curves_layout = QGridLayout(
            curves_group
        )

        curves_layout.addWidget(
            QLabel("Кривая"),
            0,
            0,
        )

        curves_layout.addWidget(
            QLabel("Видимость"),
            0,
            1,
        )

        curves_layout.addWidget(
            QLabel("Цвет"),
            0,
            2,
        )

        curves_layout.addWidget(
            QLabel("Стиль линии"),
            0,
            3,
        )

        curves_layout.addWidget(
            QLabel("Толщина"),
            0,
            4,
        )

        # ====================================================
        # RAW
        # ====================================================

        curves_layout.addWidget(
            QLabel("RAW"),
            1,
            0,
        )

        self.raw_visible = QCheckBox()

        self.raw_visible.setChecked(
            True
        )

        self.raw_visible.toggled.connect(
            self.update_curve_visibility
        )

        curves_layout.addWidget(
            self.raw_visible,
            1,
            1,
        )

        self.raw_color_button = \
            QPushButton("  ЦВЕТ  ")

        self.raw_color_button.setStyleSheet(
            self.color_button_style(
                self.raw_color
            )
        )

        self.raw_color_button.clicked.connect(
            self.choose_raw_color
        )

        curves_layout.addWidget(
            self.raw_color_button,
            1,
            2,
        )

        self.raw_style_combo = \
            QComboBox()

        self.add_line_styles(
            self.raw_style_combo
        )

        self.raw_style_combo.currentIndexChanged.connect(
            self.raw_style_changed
        )

        curves_layout.addWidget(
            self.raw_style_combo,
            1,
            3,
        )

        self.raw_width_spin = \
            QSpinBox()

        self.raw_width_spin.setRange(
            1,
            8
        )

        self.raw_width_spin.setValue(
            self.raw_width
        )

        self.raw_width_spin.valueChanged.connect(
            self.raw_width_changed
        )

        curves_layout.addWidget(
            self.raw_width_spin,
            1,
            4,
        )

        # ====================================================
        # FILTERED
        # ====================================================

        curves_layout.addWidget(
            QLabel("FILTERED"),
            2,
            0,
        )

        self.filtered_visible = \
            QCheckBox()

        self.filtered_visible.setChecked(
            True
        )

        self.filtered_visible.toggled.connect(
            self.update_curve_visibility
        )

        curves_layout.addWidget(
            self.filtered_visible,
            2,
            1,
        )

        self.filtered_color_button = \
            QPushButton("  ЦВЕТ  ")

        self.filtered_color_button.setStyleSheet(
            self.color_button_style(
                self.filtered_color
            )
        )

        self.filtered_color_button.clicked.connect(
            self.choose_filtered_color
        )

        curves_layout.addWidget(
            self.filtered_color_button,
            2,
            2,
        )

        self.filtered_style_combo = \
            QComboBox()

        self.add_line_styles(
            self.filtered_style_combo
        )

        self.filtered_style_combo.currentIndexChanged.connect(
            self.filtered_style_changed
        )

        curves_layout.addWidget(
            self.filtered_style_combo,
            2,
            3,
        )

        self.filtered_width_spin = \
            QSpinBox()

        self.filtered_width_spin.setRange(
            1,
            8
        )

        self.filtered_width_spin.setValue(
            self.filtered_width
        )

        self.filtered_width_spin.valueChanged.connect(
            self.filtered_width_changed
        )

        curves_layout.addWidget(
            self.filtered_width_spin,
            2,
            4,
        )

        # ====================================================
        # SMOOTH
        #
        # Второе значение BiiB.
        # ====================================================

        curves_layout.addWidget(
            QLabel("SMOOTH"),
            3,
            0,
        )

        self.smooth_visible = \
            QCheckBox()

        self.smooth_visible.setChecked(
            True
        )

        self.smooth_visible.toggled.connect(
            self.update_curve_visibility
        )

        curves_layout.addWidget(
            self.smooth_visible,
            3,
            1,
        )

        self.smooth_color_button = \
            QPushButton("  ЦВЕТ  ")

        self.smooth_color_button.setStyleSheet(
            self.color_button_style(
                self.smooth_color
            )
        )

        self.smooth_color_button.clicked.connect(
            self.choose_smooth_color
        )

        curves_layout.addWidget(
            self.smooth_color_button,
            3,
            2,
        )

        self.smooth_style_combo = \
            QComboBox()

        self.add_line_styles(
            self.smooth_style_combo
        )

        self.smooth_style_combo.currentIndexChanged.connect(
            self.smooth_style_changed
        )

        curves_layout.addWidget(
            self.smooth_style_combo,
            3,
            3,
        )

        self.smooth_width_spin = \
            QSpinBox()

        self.smooth_width_spin.setRange(
            1,
            8
        )

        self.smooth_width_spin.setValue(
            self.smooth_width
        )

        self.smooth_width_spin.valueChanged.connect(
            self.smooth_width_changed
        )

        curves_layout.addWidget(
            self.smooth_width_spin,
            3,
            4,
        )

        curves_layout.setColumnStretch(
            5,
            1,
        )

        main_layout.addWidget(
            curves_group
        )

        # ====================================================
        # НИЖНЯЯ ПАНЕЛЬ
        # ====================================================

        bottom_controls = QHBoxLayout()

        bottom_controls.addWidget(
            QLabel("Деление времени:")
        )

        self.x_div_combo = \
            QComboBox()

        x_divisions = [
            ("Авто", None),
            ("0,01 с", 0.01),
            ("0,02 с", 0.02),
            ("0,05 с", 0.05),
            ("0,1 с", 0.1),
            ("0,2 с", 0.2),
            ("0,5 с", 0.5),
            ("1 с", 1.0),
            ("2 с", 2.0),
            ("5 с", 5.0),
            ("10 с", 10.0),
        ]

        for text, value in x_divisions:

            self.x_div_combo.addItem(
                text,
                value
            )

        self.x_div_combo.currentIndexChanged.connect(
            self.update_x_ticks
        )

        bottom_controls.addWidget(
            self.x_div_combo
        )

        bottom_controls.addSpacing(
            20
        )

        self.diagnostics_label = QLabel(
            "Кадры: 0 | Байты: 0 | "
            "Ошибки: 0 | Точки: 0"
        )

        self.diagnostics_label.setStyleSheet(
            "color:#7f93a6;"
        )

        bottom_controls.addWidget(
            self.diagnostics_label
        )

        bottom_controls.addStretch()

        self.window_label = QLabel(
            "Окно: 0"
        )

        self.window_label.setStyleSheet(
            "color:#8fa2b5;"
        )

        bottom_controls.addWidget(
            self.window_label
        )

        main_layout.addLayout(
            bottom_controls
        )

        # ====================================================
        # НАЧАЛЬНОЕ СОСТОЯНИЕ
        # ====================================================

        self.refresh_ports()

        self.update_width_label()

        self.update_curve_visibility()

    # ========================================================
    # СТИЛИ ЛИНИЙ
    # ========================================================

    def color_button_style(self, color):

        return f"""
            QPushButton {{
                background-color: {color};
                color: #071018;
                border: 1px solid #667788;
                border-radius: 4px;
                font-weight: bold;
                padding: 4px 14px;
            }}
        """

    def add_line_styles(self, combo):

        combo.addItem(
            "Сплошная",
            Qt.SolidLine
        )

        combo.addItem(
            "Пунктир",
            Qt.DashLine
        )

        combo.addItem(
            "Точки",
            Qt.DotLine
        )

        combo.addItem(
            "Штрих-точка",
            Qt.DashDotLine
        )

        combo.addItem(
            "Штрих-двойная точка",
            Qt.DashDotDotLine
        )

    # ========================================================
    # ЦВЕТ RAW
    # ========================================================

    def choose_raw_color(self):

        color = QColorDialog.getColor(
            QColor(self.raw_color),
            self,
            "Цвет RAW",
        )

        if not color.isValid():
            return

        self.raw_color = color.name()

        self.raw_color_button.setStyleSheet(
            self.color_button_style(
                self.raw_color
            )
        )

        self.update_raw_pen()

    # ========================================================
    # ЦВЕТ FILTERED
    # ========================================================

    def choose_filtered_color(self):

        color = QColorDialog.getColor(
            QColor(self.filtered_color),
            self,
            "Цвет FILTERED",
        )

        if not color.isValid():
            return

        self.filtered_color = \
            color.name()

        self.filtered_color_button.setStyleSheet(
            self.color_button_style(
                self.filtered_color
            )
        )

        self.update_filtered_pen()

    # ========================================================
    # ЦВЕТ SMOOTH
    # ========================================================

    def choose_smooth_color(self):

        color = QColorDialog.getColor(
            QColor(self.smooth_color),
            self,
            "Цвет SMOOTH",
        )

        if not color.isValid():
            return

        self.smooth_color = color.name()

        self.smooth_color_button.setStyleSheet(
            self.color_button_style(
                self.smooth_color
            )
        )

        self.update_smooth_pen()

    # ========================================================
    # СТИЛЬ RAW
    # ========================================================

    def raw_style_changed(self):

        self.raw_style = \
            self.raw_style_combo.currentData()

        self.update_raw_pen()

    # ========================================================
    # СТИЛЬ FILTERED
    # ========================================================

    def filtered_style_changed(self):

        self.filtered_style = \
            self.filtered_style_combo.currentData()

        self.update_filtered_pen()

    # ========================================================
    # СТИЛЬ SMOOTH
    # ========================================================

    def smooth_style_changed(self):

        self.smooth_style = \
            self.smooth_style_combo.currentData()

        self.update_smooth_pen()

    # ========================================================
    # ТОЛЩИНА RAW
    # ========================================================

    def raw_width_changed(self, value):

        self.raw_width = value

        self.update_raw_pen()

    # ========================================================
    # ТОЛЩИНА FILTERED
    # ========================================================

    def filtered_width_changed(self, value):

        self.filtered_width = value

        self.update_filtered_pen()

    # ========================================================
    # ТОЛЩИНА SMOOTH
    # ========================================================

    def smooth_width_changed(self, value):

        self.smooth_width = value

        self.update_smooth_pen()

    # ========================================================
    # PEN RAW
    # ========================================================

    def update_raw_pen(self):

        self.raw_curve.setPen(
            pg.mkPen(
                color=self.raw_color,
                width=self.raw_width,
                style=self.raw_style,
            )
        )

    # ========================================================
    # PEN FILTERED
    # ========================================================

    def update_filtered_pen(self):

        self.filtered_curve.setPen(
            pg.mkPen(
                color=self.filtered_color,
                width=self.filtered_width,
                style=self.filtered_style,
            )
        )

    # ========================================================
    # PEN SMOOTH
    # ========================================================

    def update_smooth_pen(self):

        self.smooth_curve.setPen(
            pg.mkPen(
                color=self.smooth_color,
                width=self.smooth_width,
                style=self.smooth_style,
            )
        )

    # ========================================================
    # ВИДИМОСТЬ
    # ========================================================

    def update_curve_visibility(self):

        self.raw_curve.setVisible(
            self.raw_visible.isChecked()
        )

        self.filtered_curve.setVisible(
            self.filtered_visible.isChecked()
        )

        self.smooth_curve.setVisible(
            self.smooth_visible.isChecked()
        )

        self.view_changed = True
        self.data_changed = True

    # ========================================================
    # COM ПОРТЫ
    # ========================================================

    def refresh_ports(self):

        current = \
            self.port_combo.currentData()

        self.port_combo.clear()

        ports = \
            serial.tools.list_ports.comports()

        for port in ports:

            self.port_combo.addItem(
                f"{port.device} — "
                f"{port.description}",
                port.device,
            )

        if current is not None:

            index = \
                self.port_combo.findData(
                    current
                )

            if index >= 0:

                self.port_combo.setCurrentIndex(
                    index
                )

    # ========================================================
    # ПОДКЛЮЧЕНИЕ
    # ========================================================

    def toggle_connection(self):

        if self.worker.port_name is None:

            if (
                self.port_combo.currentData()
                is None
            ):

                QMessageBox.warning(
                    self,
                    "Порт",
                    "Выберите COM-порт.",
                )

                return

            port = \
                self.port_combo.currentData()

            self.status_label.setText(
                f"Подключение: {port}"
            )

            self.worker.request_connect(
                port
            )

        else:

            self.worker.request_disconnect()

    def on_connected(self):

        self.connect_button.setText(
            "ОТКЛЮЧИТЬ"
        )

        self.status_label.setText(
            f"Подключено: "
            f"{self.worker.port_name}"
        )

    def on_disconnected(self):

        self.connect_button.setText(
            "ПОДКЛЮЧИТЬ"
        )

        self.status_label.setText(
            "Отключено"
        )

    # ========================================================
    # START
    # ========================================================

    def start_measurement(self):

        self.data_count = 0
        self.filtered_count = 0

        self.reset_filter()

        self.position = 0

        self.live_follow = True
        self.measurement_active = True

        self.live_button.blockSignals(
            True
        )

        self.live_button.setChecked(
            True
        )

        self.live_button.blockSignals(
            False
        )

        self.last_slider_count = -1
        self.last_plot_left = -1
        self.last_plot_right = -1
        self.last_plot_live = None

        self.data_changed = True
        self.view_changed = True

        # ====================================================
        # ШИРИНА НЕ СБРАСЫВАЕТСЯ
        # ====================================================

        self.window_width = min(
            max(
                1,
                self.window_width
            ),
            MAX_WINDOW_POINTS
        )

        # ====================================================
        # WIDTH SLIDER
        # ====================================================

        self.width_slider.blockSignals(
            True
        )

        self.width_slider.setMinimum(
            1
        )

        self.width_slider.setMaximum(
            MAX_WINDOW_POINTS
        )

        self.width_slider.setValue(
            self.window_width
        )

        self.width_slider.blockSignals(
            False
        )

        # ====================================================
        # POSITION SLIDER
        # ====================================================

        self.position_slider.blockSignals(
            True
        )

        self.position_slider.setMinimum(
            0
        )

        self.position_slider.setMaximum(
            0
        )

        self.position_slider.setValue(
            0
        )

        self.position_slider.blockSignals(
            False
        )

        self.worker.request_start()

        self.status_label.setText(
            "ИЗМЕРЕНИЕ — ЖИВОЙ РЕЖИМ"
        )

        self.update_width_label()

        self.update_plot(
            force=True
        )

    # ========================================================
    # STOP
    # ========================================================

    def stop_measurement(self):

        self.measurement_active = False

        self.worker.request_stop()

        self.live_follow = False

        self.live_button.blockSignals(
            True
        )

        self.live_button.setChecked(
            False
        )

        self.live_button.blockSignals(
            False
        )

        self.status_label.setText(
            "ИЗМЕРЕНИЕ ОСТАНОВЛЕНО"
        )

        self.view_changed = True
        self.data_changed = True

        self.update_plot(
            force=True
        )

    # ========================================================
    # СБРОС ФИЛЬТРА
    # ========================================================

    def reset_filter(self):

        self.median_buffer.clear()

        self.butter_state = \
            sosfilt_zi(
                self.butter_sos
            )

        self.sg_buffer.clear()

    # ========================================================
    # ПРИШЛИ НОВЫЕ ДАННЫЕ
    # ========================================================

    def on_data_ready(
        self,
        t,
        values
    ):

        # ====================================================
        # BiiB:
        #
        # force_raw
        # force_smooth
        # ====================================================

        raw_value, smooth_value = \
            values

        # ====================================================
        # RAW + SMOOTH
        # ====================================================

        self.ensure_storage_capacity(
            self.data_count + 1
        )

        index = self.data_count

        self.time_data[index] = t

        self.raw_data[index] = \
            raw_value

        # Второе значение BiiB
        # сохраняем отдельно.
        self.smooth_data[index] = \
            smooth_value

        self.data_count += 1

        # ====================================================
        # MEDIAN 5
        # ====================================================

        self.median_buffer.append(
            float(raw_value)
        )

        if len(self.median_buffer) > MEDIAN_SIZE:

            del self.median_buffer[0]

        median_value = float(
            np.median(
                np.asarray(
                    self.median_buffer,
                    dtype=np.float64
                )
            )
        )

        # ====================================================
        # BUTTERWORTH
        # ====================================================

        filtered_array, \
            self.butter_state = sosfilt(
                self.butter_sos,
                [median_value],
                zi=self.butter_state,
            )

        butter_value = float(
            filtered_array[0]
        )

        # ====================================================
        # SAVITZKY-GOLAY
        # ====================================================

        self.sg_buffer.append(
            butter_value
        )

        if len(self.sg_buffer) > SG_WINDOW:

            del self.sg_buffer[0]

        if len(self.sg_buffer) == SG_WINDOW:

            sg_array = savgol_filter(
                np.asarray(
                    self.sg_buffer,
                    dtype=np.float64,
                ),
                SG_WINDOW,
                SG_POLYORDER,
            )

            center_value = float(
                sg_array[
                    self.sg_center_delay
                ]
            )

            filtered_time = (
                t
                - self.sg_center_delay
                / SAMPLE_RATE
            )

            self.ensure_storage_capacity(
                self.filtered_count + 1
            )

            fi = self.filtered_count

            self.filtered_time_data[fi] = \
                filtered_time

            self.filtered_data[fi] = \
                center_value

            self.filtered_count += 1

        # ====================================================
        # ДАННЫЕ ИЗМЕНИЛИСЬ
        #
        # В HISTORY данные продолжают сохраняться,
        # но график не перерисовывается.
        # ====================================================

        self.data_changed = True

    # ========================================================
    # LIVE
    # ========================================================

    def toggle_live(self, checked):

        if checked:

            self.live_follow = True

            self.status_label.setText(
                "ЖИВОЙ РЕЖИМ"
            )

            self.view_changed = True
            self.data_changed = True

            self.follow_latest(
                immediate=True
            )

        else:

            self.live_follow = False

            self.status_label.setText(
                "ПРОСМОТР ИСТОРИИ"
            )

            self.view_changed = True
            self.data_changed = False

            self.update_plot(
                force=True
            )

    # ========================================================
    # СЛЕДОВАТЬ ЗА ПОСЛЕДНИМИ ДАННЫМИ
    # ========================================================

    def follow_latest(
        self,
        immediate=False
    ):

        count = self.data_count

        if count <= 0:
            return

        width = min(
            self.window_width,
            count
        )

        left = max(
            0,
            count - width
        )

        self.position = left

        self.position_slider.blockSignals(
            True
        )

        self.position_slider.setMinimum(
            0
        )

        self.position_slider.setMaximum(
            max(
                0,
                count - width
            )
        )

        self.position_slider.setValue(
            left
        )

        self.position_slider.blockSignals(
            False
        )

        self.view_changed = True

        if immediate:

            self.data_changed = True

            self.update_plot(
                force=True
            )

    # ========================================================
    # POSITION
    # ========================================================

    def on_position_slider(self, value):

        if (
            not self.position_slider
            .signalsBlocked()
        ):

            if self.live_follow:

                self.live_follow = False

                self.live_button.blockSignals(
                    True
                )

                self.live_button.setChecked(
                    False
                )

                self.live_button.blockSignals(
                    False
                )

                self.status_label.setText(
                    "ПРОСМОТР ИСТОРИИ"
                )

        self.position = int(value)

        self.view_changed = True
        self.data_changed = False

        self.update_plot(
            force=True
        )

    # ========================================================
    # WIDTH
    # ========================================================

    def on_width_slider(self, value):

        self.window_width = min(
            max(
                1,
                int(value)
            ),
            MAX_WINDOW_POINTS
        )

        self.update_width_label()

        count = self.data_count

        if count <= 0:

            self.view_changed = True

            return

        # ====================================================
        # LIVE
        # ====================================================

        if self.live_follow:

            self.follow_latest(
                immediate=False
            )

        # ====================================================
        # HISTORY
        # ====================================================

        else:

            max_position = max(
                0,
                count - self.window_width
            )

            self.position = min(
                self.position,
                max_position
            )

            self.position_slider.blockSignals(
                True
            )

            self.position_slider.setMinimum(
                0
            )

            self.position_slider.setMaximum(
                max_position
            )

            self.position_slider.setValue(
                self.position
            )

            self.position_slider.blockSignals(
                False
            )

        self.view_changed = True
        self.data_changed = False

        self.update_plot(
            force=True
        )

    # ========================================================
    # ПОДПИСЬ ШИРИНЫ
    # ========================================================

    def update_width_label(self):

        seconds = (
            self.window_width
            / SAMPLE_RATE
        )

        self.width_value_label.setText(
            f"{seconds:.3f} с"
        )

    # ========================================================
    # ДИАПАЗОНЫ СЛАЙДЕРОВ
    # ========================================================

    def update_slider_ranges(self):

        count = self.data_count

        if count <= 0:
            return

        width = min(
            self.window_width,
            MAX_WINDOW_POINTS
        )

        max_position = max(
            0,
            count - width
        )

        # ====================================================
        # LIVE
        # ====================================================

        if self.live_follow:

            self.position = max_position

            self.position_slider.blockSignals(
                True
            )

            self.position_slider.setMinimum(
                0
            )

            self.position_slider.setMaximum(
                max_position
            )

            self.position_slider.setValue(
                self.position
            )

            self.position_slider.blockSignals(
                False
            )

            return

        # ====================================================
        # HISTORY
        #
        # Новые данные увеличивают доступную историю,
        # но текущая позиция не двигается автоматически.
        # ====================================================

        self.position = min(
            self.position,
            max_position
        )

        self.position_slider.blockSignals(
            True
        )

        self.position_slider.setMinimum(
            0
        )

        self.position_slider.setMaximum(
            max_position
        )

        self.position_slider.setValue(
            self.position
        )

        self.position_slider.blockSignals(
            False
        )

    # ========================================================
    # DISPLAY DOWNSAMPLING
    # ========================================================

    @staticmethod
    def downsample_minmax(
        x,
        y,
        max_points=MAX_DISPLAY_POINTS
    ):

        n = len(y)

        if n <= max_points:

            return x, y

        bucket_count = max(
            1,
            max_points // 2
        )

        edges = np.linspace(
            0,
            n,
            bucket_count + 1,
            dtype=np.int64
        )

        starts = edges[:-1]
        ends = edges[1:]

        valid = ends > starts

        starts = starts[valid]
        ends = ends[valid]

        if len(starts) == 0:

            return x, y

        out_x = np.empty(
            len(starts) * 2,
            dtype=x.dtype
        )

        out_y = np.empty(
            len(starts) * 2,
            dtype=y.dtype
        )

        out_index = 0

        for start, end in zip(
            starts,
            ends
        ):

            yy = y[start:end]

            if len(yy) == 0:
                continue

            local_min = int(
                np.argmin(yy)
            )

            local_max = int(
                np.argmax(yy)
            )

            imin = start + local_min
            imax = start + local_max

            if imin <= imax:

                out_x[out_index] = \
                    x[imin]

                out_y[out_index] = \
                    y[imin]

                out_x[out_index + 1] = \
                    x[imax]

                out_y[out_index + 1] = \
                    y[imax]

            else:

                out_x[out_index] = \
                    x[imax]

                out_y[out_index] = \
                    y[imax]

                out_x[out_index + 1] = \
                    x[imin]

                out_y[out_index + 1] = \
                    y[imin]

            out_index += 2

        return (
            out_x[:out_index],
            out_y[:out_index]
        )

    # ========================================================
    # АВТОМАТИЧЕСКИЙ Y-МАСШТАБ
    # ========================================================

    def update_y_range(
        self,
        visible_arrays
    ):

        """
        Автоматический вертикальный масштаб.

        Используются ТОЛЬКО данные текущего
        видимого окна.

        Если видны RAW, SMOOTH и FILTERED,
        учитываются все три кривые.

        После определения реального диапазона
        добавляется фиксированный запас 10%
        сверху и снизу.
        """

        if not visible_arrays:
            return

        ymin = np.inf
        ymax = -np.inf

        for arr in visible_arrays:

            if arr is None:
                continue

            if len(arr) == 0:
                continue

            finite = arr[
                np.isfinite(arr)
            ]

            if len(finite) == 0:
                continue

            local_min = float(
                np.min(finite)
            )

            local_max = float(
                np.max(finite)
            )

            if local_min < ymin:
                ymin = local_min

            if local_max > ymax:
                ymax = local_max

        if (
            not np.isfinite(ymin)
            or not np.isfinite(ymax)
        ):

            return

        # ====================================================
        # Одинаковое значение
        # ====================================================

        if ymax <= ymin:

            center = (
                ymin + ymax
            ) * 0.5

            margin = max(
                1.0,
                abs(center) * 0.05
            )

            self.plot.setYRange(
                center - margin,
                center + margin,
                padding=0,
            )

            return

        # ====================================================
        # Нормальный диапазон
        # ====================================================

        data_range = ymax - ymin

        margin = (
            data_range
            * Y_AXIS_MARGIN
        )

        self.plot.setYRange(
            ymin - margin,
            ymax + margin,
            padding=0,
        )

    # ========================================================
    # ОБНОВЛЕНИЕ ГРАФИКА
    # ========================================================

    def update_plot(
        self,
        force=False
    ):

        count = self.data_count

        if count <= 0:

            self.raw_curve.setData(
                [],
                []
            )

            self.smooth_curve.setData(
                [],
                []
            )

            self.filtered_curve.setData(
                [],
                []
            )

            return

        # ====================================================
        # HISTORY
        #
        # Новые данные не перерисовывают график,
        # если пользователь ничего не менял.
        # ====================================================

        if (
            not force
            and not self.live_follow
            and not self.view_changed
        ):

            self.update_slider_ranges()

            self.data_changed = False

            return

        # ====================================================
        # LIVE
        # ====================================================

        if (
            not force
            and self.live_follow
            and not self.data_changed
            and not self.view_changed
        ):

            return

        # ====================================================
        # СЛАЙДЕРЫ
        # ====================================================

        self.update_slider_ranges()

        # ====================================================
        # ОПРЕДЕЛЕНИЕ ВИДИМОГО ОКНА
        # ====================================================

        width = min(
            self.window_width,
            count
        )

        if self.live_follow:

            left = max(
                0,
                count - width
            )

            right = count

            self.position = left

        else:

            max_position = max(
                0,
                count - width
            )

            left = min(
                self.position,
                max_position
            )

            right = min(
                count,
                left + width
            )

            self.position = left

        # ====================================================
        # RAW VIEW
        # ====================================================

        raw_time_view = \
            self.time_data[
                left:right
            ]

        raw_values_view = \
            self.raw_data[
                left:right
            ]

        # ====================================================
        # SMOOTH VIEW
        #
        # Временная ось та же самая, поскольку RAW и SMOOTH
        # приходят в одном кадре BiiB.
        # ====================================================

        smooth_values_view = \
            self.smooth_data[
                left:right
            ]

        # ====================================================
        # RAW DISPLAY DECIMATION
        # ====================================================

        if (
            len(raw_values_view)
            > MAX_DISPLAY_POINTS
        ):

            raw_plot_x, \
            raw_plot_y = \
                self.downsample_minmax(
                    raw_time_view,
                    raw_values_view,
                    MAX_DISPLAY_POINTS
                )

        else:

            raw_plot_x = raw_time_view
            raw_plot_y = raw_values_view

        # ====================================================
        # SMOOTH DISPLAY DECIMATION
        # ====================================================

        if (
            len(smooth_values_view)
            > MAX_DISPLAY_POINTS
        ):

            smooth_plot_x, \
            smooth_plot_y = \
                self.downsample_minmax(
                    raw_time_view,
                    smooth_values_view,
                    MAX_DISPLAY_POINTS
                )

        else:

            smooth_plot_x = raw_time_view
            smooth_plot_y = smooth_values_view

        # ====================================================
        # RAW
        # ====================================================

        if self.raw_visible.isChecked():

            self.raw_curve.setData(
                raw_plot_x,
                raw_plot_y,
                connect="finite",
            )

        else:

            self.raw_curve.setData(
                [],
                []
            )

        # ====================================================
        # SMOOTH
        #
        # force_smooth непосредственно от STM32.
        # ====================================================

        if self.smooth_visible.isChecked():

            self.smooth_curve.setData(
                smooth_plot_x,
                smooth_plot_y,
                connect="finite",
            )

        else:

            self.smooth_curve.setData(
                [],
                []
            )

        # ====================================================
        # FILTERED
        # ====================================================

        filtered_plot_x = None
        filtered_plot_y = None

        if (
            self.filtered_visible.isChecked()
            and self.filtered_count > 0
        ):

            ft_all = \
                self.filtered_time_data[
                    :self.filtered_count
                ]

            fd_all = \
                self.filtered_data[
                    :self.filtered_count
                ]

            if len(raw_time_view) > 0:

                t_left = float(
                    raw_time_view[0]
                )

                t_right = float(
                    raw_time_view[-1]
                )

                f_left = np.searchsorted(
                    ft_all,
                    t_left,
                    side="left"
                )

                f_right = np.searchsorted(
                    ft_all,
                    t_right,
                    side="right"
                )

                f_left = max(
                    0,
                    min(
                        f_left,
                        len(ft_all)
                    )
                )

                f_right = max(
                    f_left,
                    min(
                        f_right,
                        len(ft_all)
                    )
                )

                ft_view = ft_all[
                    f_left:f_right
                ]

                fd_view = fd_all[
                    f_left:f_right
                ]

                if (
                    len(fd_view)
                    > MAX_DISPLAY_POINTS
                ):

                    filtered_plot_x, \
                    filtered_plot_y = \
                        self.downsample_minmax(
                            ft_view,
                            fd_view,
                            MAX_DISPLAY_POINTS
                        )

                else:

                    filtered_plot_x = \
                        ft_view

                    filtered_plot_y = \
                        fd_view

            else:

                filtered_plot_x = \
                    np.empty(
                        0,
                        dtype=np.float64
                    )

                filtered_plot_y = \
                    np.empty(
                        0,
                        dtype=np.float64
                    )

            self.filtered_curve.setData(
                filtered_plot_x,
                filtered_plot_y,
                connect="finite",
            )

        else:

            self.filtered_curve.setData(
                [],
                []
            )

        # ====================================================
        # X RANGE
        # ====================================================

        if len(raw_time_view) > 0:

            x_left = float(
                raw_time_view[0]
            )

            x_right = float(
                raw_time_view[-1]
            )

            if x_right <= x_left:

                x_right = (
                    x_left
                    + 1.0 / SAMPLE_RATE
                )

            self.plot.setXRange(
                x_left,
                x_right,
                padding=0,
            )

        # ====================================================
        # Y RANGE
        #
        # Берём именно отображаемые данные:
        #
        # RAW
        # SMOOTH
        # FILTERED
        #
        # только текущего окна.
        # ====================================================

        visible_arrays = []

        if (
            self.raw_visible.isChecked()
            and len(raw_plot_y) > 0
        ):

            visible_arrays.append(
                raw_plot_y
            )

        if (
            self.smooth_visible.isChecked()
            and len(smooth_plot_y) > 0
        ):

            visible_arrays.append(
                smooth_plot_y
            )

        if (
            self.filtered_visible.isChecked()
            and filtered_plot_y is not None
            and len(filtered_plot_y) > 0
        ):

            visible_arrays.append(
                filtered_plot_y
            )

        self.update_y_range(
            visible_arrays
        )

        # ====================================================
        # ОПИСАНИЕ ОКНА
        # ====================================================

        if len(raw_time_view) > 0:

            x_left = float(
                raw_time_view[0]
            )

            x_right = float(
                raw_time_view[-1]
            )

            visible_seconds = (
                len(raw_time_view)
                / SAMPLE_RATE
            )

            self.window_label.setText(
                f"Окно: "
                f"{x_left:.3f} → "
                f"{x_right:.3f} с   |   "
                f"{visible_seconds:.3f} с   |   "
                f"{len(raw_time_view):,} точек"
            )

        # ====================================================
        # СОСТОЯНИЕ
        # ====================================================

        self.last_plot_left = left
        self.last_plot_right = right

        self.last_plot_live = \
            self.live_follow

        self.view_changed = False
        self.data_changed = False

    # ========================================================
    # ДЕЛЕНИЯ ОСИ X
    # ========================================================

    def update_x_ticks(self):

        value = \
            self.x_div_combo.currentData()

        axis = \
            self.plot.getAxis(
                "bottom"
            )

        if value is None:

            axis.setTickSpacing(
                major=None,
                minor=None,
            )

            return

        axis.setTickSpacing(
            major=value,
            minor=value / 5.0,
        )

    # ========================================================
    # СТАТУС
    # ========================================================

    def on_worker_status(self, text):

        self.status_label.setText(
            text
        )

    # ========================================================
    # ДИАГНОСТИКА
    # ========================================================

    def on_diagnostics(self, data):

        frames = data.get(
            "frames",
            0
        )

        bytes_count = data.get(
            "bytes",
            0
        )

        bad = data.get(
            "bad",
            0
        )

        points = self.data_count

        self.diagnostics_label.setText(
            f"Кадры: {frames:,}   |   "
            f"Байты: {bytes_count:,}   |   "
            f"Ошибки: {bad:,}   |   "
            f"Точки: {points:,}"
        )

    # ========================================================
    # ЗАКРЫТИЕ
    # ========================================================

    def closeEvent(self, event):

        self.update_timer.stop()

        self.worker.request_shutdown()

        self.worker_thread.quit()

        if not self.worker_thread.wait(
            2000
        ):

            self.worker_thread.terminate()

            self.worker_thread.wait()

        event.accept()


# ============================================================
# MAIN
# ============================================================

def main():

    app = QApplication(
        sys.argv
    )

    apply_dark_style(app)

    pg.setConfigOptions(
        antialias=True,
        background="#070b10",
        foreground="#d9e4ef",
    )

    window = MainWindow()

    window.show()

    sys.exit(
        app.exec_()
    )


if __name__ == "__main__":

    main()
