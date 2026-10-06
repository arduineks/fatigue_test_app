import sys
import time
import math
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
    QMenu,
    QActionGroup, QColorDialog,
    QLineEdit,
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

MEASUREMENT_FRAME_FORMAT = "<BiiffB"
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

        # =================================================
        # DATA
        # =================================================

        self.values = []
        self.raw_values = []
        self.filtered_values = []

        self.running = False
        self.start_time = None

        # Максимальная история.
        # 300000 / 330 SPS ≈ 15 минут.
        self.max_points = 300000

        # Частота обновления GUI.
        self.update_interval_ms = 40

        self._update_pending = False

        # =================================================
        # WIDGET
        # =================================================

        self.setMinimumWidth(500)
        self.setMinimumHeight(300)

        # =================================================
        # COLORS
        # =================================================

        self.background_color = QColor("#0B0F14")
        self.grid_color = QColor("#27313A")
        self.axis_color = QColor("#71808C")
        self.text_color = QColor("#F2F5F7")

        self.force_color = QColor("#19E6FF")
        self.raw_color = QColor("#FFD400")
        self.filtered_color = QColor("#FF6B6B")

        # Callback изменения цвета FORCE_N
        self.on_force_color_changed = None

        # =================================================
        # VISIBILITY
        # =================================================

        self.force_visible = True
        self.raw_visible = False
        self.filtered_visible = False

        # =================================================
        # LINE STYLE
        # =================================================

        self.force_style = Qt.SolidLine
        self.raw_style = Qt.DashLine
        self.filtered_style = Qt.DotLine

        # =================================================
        # LIVE / HISTORY
        # =================================================

        self.live_mode = True

        # Ширина отображаемого окна в секундах.
        self.x_window_seconds = 5.0

        # Частота измерения.
        self.sample_rate = 330.0

        # Индекс первого отображаемого кадра.
        self.view_start = 0

        # =================================================
        # Y SCALE
        # =================================================

        # Шаг горизонтальной сетки и шкалы Y.
        self.y_grid_step = 0.5

        # Дополнительный запас сверху.
        self.y_margin = 1.0

        # Текущий максимум шкалы Y.
        #
        # ВАЖНО:
        # Он не уменьшается при движении по истории.
        # При появлении нового максимума шкала расширяется.
        self.y_max = 1.0

        # =================================================
        # COMPACT CONTROLS
        # =================================================

        self.control_height = 20

        self._force_control_rect = QRectF()
        self._raw_control_rect = QRectF()
        self._filtered_control_rect = QRectF()

        self._live_rect = QRectF()
        self._zoom_rect = QRectF()

        # =================================================
        # SCROLLBAR
        # =================================================

        self.scrollbar_height = 12

        self._scroll_dragging = False
        self._scroll_drag_offset = 0

        # =================================================
        # UPDATE TIMER
        # =================================================

        self.update_timer = QTimer(self)

        self.update_timer.setInterval(
            self.update_interval_ms
        )

        self.update_timer.timeout.connect(
            self._refresh_graph
        )

        self.update_timer.start()

        # =================================================
        # STYLE MENU
        # =================================================

        self.color_options = {
            "Голубой": "#19E6FF",
            "Жёлтый": "#FFD400",
            "Красный": "#FF6B6B",
            "Зелёный": "#39FF88",
            "Белый": "#F2F5F7",
            "Оранжевый": "#FF9F43",
            "Фиолетовый": "#B388FF",
            "Розовый": "#FF66CC",
        }

        self.style_options = {
            "Сплошная": Qt.SolidLine,
            "Пунктир": Qt.DashLine,
            "Точечная": Qt.DotLine,
            "Штрих-точка": Qt.DashDotLine,
            "Штрих-две точки": Qt.DashDotDotLine,
        }

        # =================================================
        # CYCLE ANALYSIS
        # =================================================

        self.cycle_count = 0
        self.cycle_max = None
        self.cycle_min = None
        self.cycle_mid = None
        self.cycle_amplitude = None

        self.cycle_state = "SEARCH_DIRECTION"
        self.cycle_prev_force = None
        self.cycle_candidate_max = None
        self.cycle_candidate_min = None
        self.cycle_max_value = None
        self.cycle_min_value = None
        self.cycle_mid_value = None
        self.cycle_tolerance = 0.03
        self.cycle_turn_confirm = 2
        self.cycle_turn_count = 0
        self.cycle_last_direction = 0

        self.cycle_max_visible = True
        self.cycle_mid_visible = True
        self.cycle_min_visible = True
        self.cycle_corridor_visible = True

        self.cycle_max_color = QColor("#FF4D4D")
        self.cycle_mid_color = QColor("#FFFFFF")
        self.cycle_min_color = QColor("#4D9FFF")
        self.cycle_corridor_color = QColor("#19E6FF")

        self._cycle_max_rect = QRectF()
        self._cycle_mid_rect = QRectF()
        self._cycle_min_rect = QRectF()
        self._cycle_corridor_rect = QRectF()

    # =====================================================
    # Y SCALE
    # =====================================================

    def reset_y_scale(self):

        self.y_max = 1.0

    def update_y_scale(self):

        if not self.values:
            self.y_max = 1.0
            return

        current_max = max(
            self.values
        )

        # Сила ниже нуля не должна влиять
        # на положительную шкалу.
        current_max = max(
            0.0,
            current_max,
        )

        # Максимум + 1 N.
        required_max = (
                current_max
                + self.y_margin
        )

        # Округляем вверх до ближайшего
        # значения 0.5 N.
        required_max = (
                math.ceil(
                    required_max
                    / self.y_grid_step
                )
                * self.y_grid_step
        )

        # Масштаб только расширяется.
        if required_max > self.y_max:
            self.y_max = required_max

        # Минимальный диапазон.
        self.y_max = max(
            self.y_grid_step * 2,
            self.y_max,
        )

    # =====================================================
    # MEASUREMENT
    # =====================================================

    def start_measurement(self):

        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()

        self.running = True
        self.start_time = time.time()

        self.view_start = 0
        self.live_mode = True

        self.reset_cycle_analysis()

        self.reset_y_scale()

        self.update()

    # =====================================================

    def stop_measurement(self):

        self.running = False

        self.update()

    # =====================================================

    def clear(self):

        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()

        self.running = False
        self.start_time = None

        self.view_start = 0
        self.live_mode = True

        self.reset_cycle_analysis()

        self.reset_y_scale()

        self.update()

    # =====================================================
    # VISIBILITY
    # =====================================================

    def set_force_visible(self, visible):

        self.force_visible = bool(visible)

        self.update()

    # =====================================================

    def set_raw_visible(self, visible):

        self.raw_visible = bool(visible)

        self.update()

    # =====================================================

    def set_filtered_visible(self, visible):

        self.filtered_visible = bool(visible)

        self.update()

    # =====================================================
    # ADD FRAME
    # =====================================================

    def add_frame(
            self,
            raw,
            filtered,
            force_n,
    ):

        try:

            raw = float(raw)
            filtered = float(filtered)
            force_n = float(force_n)

        except (TypeError, ValueError):

            return

        self.raw_values.append(raw)
        self.filtered_values.append(filtered)
        self.values.append(force_n)

        if self.running:
            self.process_cycle(force_n)

        # -------------------------------------------------
        # UPDATE Y SCALE
        # -------------------------------------------------

        if force_n > 0:

            required_max = (
                    force_n
                    + self.y_margin
            )

            required_max = (
                    math.ceil(
                        required_max
                        / self.y_grid_step
                    )
                    * self.y_grid_step
            )

            if required_max > self.y_max:
                self.y_max = required_max

        # -------------------------------------------------
        # Limit total history.
        # -------------------------------------------------

        if len(self.values) > self.max_points:
            remove_count = (
                    len(self.values)
                    - self.max_points
            )

            del self.values[
                :remove_count
                ]

            del self.raw_values[
                :remove_count
                ]

            del self.filtered_values[
                :remove_count
                ]

            self.view_start = max(
                0,
                self.view_start - remove_count,
            )

        # -------------------------------------------------
        # LIVE follows the end automatically.
        # -------------------------------------------------

        if self.live_mode:
            self.view_start = (
                self.get_live_start()
            )

        # =====================================================
        # CYCLE ANALYSIS
        # =====================================================

    def reset_cycle_analysis(self):

        self.cycle_count = 0
        self.cycle_max = None
        self.cycle_min = None
        self.cycle_mid = None
        self.cycle_amplitude = None

        self.cycle_state = "SEARCH_DIRECTION"
        self.cycle_prev_force = None
        self.cycle_candidate_max = None
        self.cycle_candidate_min = None
        self.cycle_max_value = None
        self.cycle_min_value = None
        self.cycle_mid_value = None
        self.cycle_turn_count = 0
        self.cycle_last_direction = 0

    def process_cycle(self, force):

        force = float(force)

        if self.cycle_prev_force is None:
            self.cycle_prev_force = force
            self.cycle_candidate_max = force
            self.cycle_candidate_min = force
            return

        delta = force - self.cycle_prev_force

        if abs(delta) < self.cycle_tolerance * 0.15:
            self.cycle_prev_force = force
            return

        direction = 1 if delta > 0 else -1

        # Первое реальное направление движения.
        if self.cycle_last_direction == 0:
            self.cycle_last_direction = direction
            self.cycle_turn_count = 0
            if direction > 0:
                self.cycle_candidate_max = force
            else:
                self.cycle_candidate_min = force
            self.cycle_prev_force = force
            return

        # -------------------------------------------------
        # Движение вверх: ищем максимум.
        # -------------------------------------------------
        if self.cycle_last_direction > 0:

            if force >= self.cycle_candidate_max:
                self.cycle_candidate_max = force
                self.cycle_turn_count = 0
            elif direction < 0:
                self.cycle_turn_count += 1

                if self.cycle_turn_count >= self.cycle_turn_confirm:
                    high = self.cycle_candidate_max

                    if self.cycle_min_value is None:
                        self.cycle_max_value = high
                    else:
                        self.cycle_max_value = high

                    self.cycle_candidate_min = force
                    self.cycle_last_direction = -1
                    self.cycle_turn_count = 0

        # -------------------------------------------------
        # Движение вниз: ищем минимум.
        # -------------------------------------------------
        else:

            if force <= self.cycle_candidate_min:
                self.cycle_candidate_min = force
                self.cycle_turn_count = 0
            elif direction > 0:
                self.cycle_turn_count += 1

                if self.cycle_turn_count >= self.cycle_turn_confirm:
                    low = self.cycle_candidate_min

                    if self.cycle_max_value is not None:
                        self.cycle_min_value = low

                        if (
                                self.cycle_max_value
                                - self.cycle_min_value
                                >= self.cycle_tolerance
                        ):
                            self.cycle_mid_value = (
                                                           self.cycle_max_value
                                                           + self.cycle_min_value
                                                   ) / 2.0

                            self.cycle_state = "WAIT_MID_UP"

                    self.cycle_candidate_max = force
                    self.cycle_last_direction = 1
                    self.cycle_turn_count = 0

        # -------------------------------------------------
        # MID -> MAX -> MID -> MIN -> MID
        # -------------------------------------------------
        # После первого найденного MAX/MIN получаем реальный MID.
        # Далее цикл фиксируется при прохождении MID вверх после MIN.
        if (
                self.cycle_state == "WAIT_MID_UP"
                and self.cycle_mid_value is not None
                and self.cycle_min_value is not None
        ):
            if (
                    self.cycle_prev_force
                    < self.cycle_mid_value
                    and force >= self.cycle_mid_value
            ):
                self.cycle_count += 1
                self.cycle_max = self.cycle_max_value
                self.cycle_min = self.cycle_min_value
                self.cycle_mid = self.cycle_mid_value
                self.cycle_amplitude = (
                                               self.cycle_max
                                               - self.cycle_min
                                       ) / 2.0

                # Начинаем новый цикл с уже известного MID.
                self.cycle_max_value = force
                self.cycle_min_value = force
                self.cycle_candidate_max = force
                self.cycle_candidate_min = force
                self.cycle_state = "SEARCH_DIRECTION"
                self.cycle_last_direction = 1
                self.cycle_turn_count = 0

        self.cycle_prev_force = force

        # =====================================================
        # CYCLE CONTROLS
        # =====================================================

    def set_cycle_max_visible(self, visible):
        self.cycle_max_visible = bool(visible)
        self.update()

    def set_cycle_mid_visible(self, visible):
        self.cycle_mid_visible = bool(visible)
        self.update()

    def set_cycle_min_visible(self, visible):
        self.cycle_min_visible = bool(visible)
        self.update()

    def set_cycle_corridor_visible(self, visible):
        self.cycle_corridor_visible = bool(visible)
        self.update()

    def get_cycle_color(self, name):
        if name == "MAX":
            return self.cycle_max_color
        if name == "MID":
            return self.cycle_mid_color
        if name == "MIN":
            return self.cycle_min_color
        if name == "CORRIDOR":
            return self.cycle_corridor_color
        return QColor("#FFFFFF")

    def set_cycle_color(self, name, value):
        color = QColor(value)
        if not color.isValid():
            return
        if name == "MAX":
            self.cycle_max_color = color
        elif name == "MID":
            self.cycle_mid_color = color
        elif name == "MIN":
            self.cycle_min_color = color
        elif name == "CORRIDOR":
            self.cycle_corridor_color = color
        self.update()

    def show_cycle_color_menu(self, name, global_pos):
        color = QColorDialog.getColor(
            self.get_cycle_color(name),
            self,
            f"Цвет {name}"
        )
        if color.isValid():
            self.set_cycle_color(name, color.name())

        # =====================================================
        # DRAW CYCLE ANALYSIS
        # =====================================================

    def draw_cycle_analysis(self, painter, plot, force_min, force_max):

        if self.cycle_max is None:
            return

        value_range = force_max - force_min
        if value_range <= 0:
            return

        def y_for(value):
            return (
                    plot.bottom()
                    - (
                            (value - force_min)
                            / value_range
                    ) * plot.height()
            )

        y_max = y_for(self.cycle_max)
        y_mid = y_for(self.cycle_mid)
        y_min = y_for(self.cycle_min)

        if self.cycle_corridor_visible:
            corridor = QColor(self.cycle_corridor_color)
            corridor.setAlpha(32)
            painter.fillRect(
                QRectF(
                    plot.left(),
                    min(y_max, y_min),
                    plot.width(),
                    abs(y_min - y_max),
                ),
                corridor,
            )

        if self.cycle_max_visible:
            painter.setPen(
                QPen(
                    self.cycle_max_color,
                    1.5,
                    Qt.DashLine,
                )
            )
            painter.drawLine(
                int(plot.left()), int(y_max),
                int(plot.right()), int(y_max),
            )

        if self.cycle_mid_visible:
            painter.setPen(
                QPen(
                    self.cycle_mid_color,
                    1.5,
                    Qt.DashDotLine,
                )
            )
            painter.drawLine(
                int(plot.left()), int(y_mid),
                int(plot.right()), int(y_mid),
            )

        if self.cycle_min_visible:
            painter.setPen(
                QPen(
                    self.cycle_min_color,
                    1.5,
                    Qt.DashLine,
                )
            )
            painter.drawLine(
                int(plot.left()), int(y_min),
                int(plot.right()), int(y_min),
            )

    # =====================================================
    # OLD COMPATIBILITY
    # =====================================================

    def add_value(self, value):

        try:

            value = float(value)

        except (TypeError, ValueError):

            return

        self.values.append(value)

        # -------------------------------------------------
        # UPDATE Y SCALE
        # -------------------------------------------------

        if value > 0:

            required_max = (
                    value
                    + self.y_margin
            )

            required_max = (
                    math.ceil(
                        required_max
                        / self.y_grid_step
                    )
                    * self.y_grid_step
            )

            if required_max > self.y_max:
                self.y_max = required_max

        # -------------------------------------------------
        # Limit history.
        # -------------------------------------------------

        if len(self.values) > self.max_points:
            remove_count = (
                    len(self.values)
                    - self.max_points
            )

            del self.values[
                :remove_count
                ]

            self.view_start = max(
                0,
                self.view_start - remove_count,
            )

        if self.live_mode:
            self.view_start = (
                self.get_live_start()
            )

    # =====================================================
    # RANGE
    # =====================================================

    @staticmethod
    def get_range(values):

        if not values:
            return 0.0, 1.0

        min_value = min(values)
        max_value = max(values)

        if min_value == max_value:

            margin = max(
                abs(min_value) * 0.1,
                0.1,
            )

            min_value -= margin
            max_value += margin

        else:

            margin = (
                             max_value
                             - min_value
                     ) * 0.10

            min_value -= margin
            max_value += margin

        return min_value, max_value

    # =====================================================
    # VISIBLE POINT COUNT
    # =====================================================

    def get_visible_count(self):

        count = int(
            self.x_window_seconds
            * self.sample_rate
        )

        return max(
            2,
            count,
        )

    # =====================================================
    # LIVE START
    # =====================================================

    def get_live_start(self):

        total = len(
            self.values
        )

        visible_count = (
            self.get_visible_count()
        )

        return max(
            0,
            total - visible_count,
        )

    # =====================================================
    # VISIBLE RANGE
    # =====================================================

    def get_visible_range(self):

        total = len(
            self.values
        )

        if total == 0:
            return 0, 0

        visible_count = (
            self.get_visible_count()
        )

        if self.live_mode:

            start = max(
                0,
                total - visible_count,
            )

        else:

            max_start = max(
                0,
                total - visible_count,
            )

            start = min(
                max(0, self.view_start),
                max_start,
            )

        end = min(
            total,
            start + visible_count,
        )

        return start, end

    # =====================================================
    # DRAW SIGNAL
    # =====================================================

    def draw_signal(
            self,
            painter,
            plot,
            values,
            color,
            style,
            min_value=None,
            max_value=None,
            width=2,
            start_index=0,
            end_index=None,
    ):

        if not values:
            return

        if end_index is None:
            end_index = len(
                values
            )

        if end_index - start_index < 2:
            return

        visible_values = values[
                         start_index:end_index
                         ]

        if len(
                visible_values
        ) < 2:
            return

        if (
                min_value is None
                or max_value is None
        ):
            min_value, max_value = (
                self.get_range(
                    visible_values
                )
            )

        value_range = (
                max_value
                - min_value
        )

        if value_range <= 0:
            value_range = 1.0

        painter.setPen(
            QPen(
                color,
                width,
                style,
            )
        )

        count = len(
            visible_values
        )

        for i in range(
                1,
                count
        ):
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

            y1 = (
                    plot.bottom()
                    - (
                            (
                                    visible_values[i - 1]
                                    - min_value
                            )
                            / value_range
                    )
                    * plot.height()
            )

            y2 = (
                    plot.bottom()
                    - (
                            (
                                    visible_values[i]
                                    - min_value
                            )
                            / value_range
                    )
                    * plot.height()
            )

            painter.drawLine(
                int(x1),
                int(y1),
                int(x2),
                int(y2),
            )

    # =====================================================
    # GUI UPDATE
    # =====================================================

    def _refresh_graph(self):

        if self.values:

            if self.live_mode:
                self.view_start = (
                    self.get_live_start()
                )

            self.update()

    # =====================================================
    # LIVE
    # =====================================================

    def set_live_mode(self, enabled):

        self.live_mode = bool(
            enabled
        )

        if self.live_mode:
            self.view_start = (
                self.get_live_start()
            )

        self.update()

    # =====================================================
    # X SCALE
    # =====================================================

    def set_x_window(self, seconds):

        try:

            seconds = float(
                seconds
            )

        except (
                TypeError,
                ValueError
        ):

            return

        self.x_window_seconds = max(
            0.1,
            seconds,
        )

        if self.live_mode:

            self.view_start = (
                self.get_live_start()
            )

        else:

            total = len(
                self.values
            )

            max_start = max(
                0,
                total
                - self.get_visible_count(),
            )

            self.view_start = min(
                self.view_start,
                max_start,
            )

        self.update()

    # =====================================================
    # SCROLLBAR GEOMETRY
    # =====================================================

    def get_scrollbar_geometry(self):

        x = 70

        width = max(
            10,
            self.width() - 95,
        )

        y = (
                self.height()
                - 18
        )

        return QRectF(
            x,
            y,
            width,
            self.scrollbar_height,
        )

    # =====================================================
    # DRAW SCROLLBAR
    # =====================================================

    def draw_scrollbar(
            self,
            painter,
    ):

        scrollbar = (
            self.get_scrollbar_geometry()
        )

        # Track

        painter.setPen(
            QPen(
                self.grid_color,
                1,
            )
        )

        painter.setBrush(
            QColor("#11171D")
        )

        painter.drawRoundedRect(
            scrollbar,
            4,
            4,
        )

        total = len(
            self.values
        )

        visible_count = (
            self.get_visible_count()
        )

        if total <= 0:
            return

        # -------------------------------------------------
        # Everything fits.
        # -------------------------------------------------

        if total <= visible_count:

            handle = QRectF(
                scrollbar
            )

        else:

            ratio = (
                    visible_count
                    / total
            )

            handle_width = max(
                30,
                scrollbar.width()
                * ratio,
            )

            max_start = (
                    total
                    - visible_count
            )

            start_ratio = (
                self.view_start
                / max_start
                if max_start > 0
                else 0
            )

            handle_x = (
                    scrollbar.left()
                    + (
                            scrollbar.width()
                            - handle_width
                    )
                    * start_ratio
            )

            handle = QRectF(
                handle_x,
                scrollbar.top(),
                handle_width,
                scrollbar.height(),
            )

        painter.setPen(
            QPen(
                self.axis_color,
                1,
            )
        )

        painter.setBrush(
            QColor("#35424D")
            if not self.live_mode
            else QColor("#19E6FF")
        )

        painter.drawRoundedRect(
            handle,
            4,
            4,
        )

    # =====================================================
    # CONTROLS
    # =====================================================

    def draw_controls(
            self,
            painter,
            plot,
    ):

        # -------------------------------------------------
        # Graph signal controls
        # -------------------------------------------------

        total_width = 220

        controls_x = (
                int(plot.right())
                - total_width
                - 5
        )

        controls_y = (
                int(plot.top())
                + 3
        )

        def draw_button(
                x,
                width,
                text,
                checked,
                color,
        ):

            button_rect = QRectF(
                x,
                controls_y,
                width,
                self.control_height,
            )

            if checked:

                painter.setBrush(
                    QColor("#151B21")
                )

                painter.setPen(
                    QPen(
                        color,
                        1,
                    )
                )

                text_color = color

            else:

                painter.setBrush(
                    QColor("#0B0F14")
                )

                painter.setPen(
                    QPen(
                        self.axis_color,
                        1,
                    )
                )

                text_color = (
                    self.text_color
                )

            painter.drawRoundedRect(
                button_rect,
                3,
                3,
            )

            painter.setPen(
                QPen(
                    text_color,
                    1,
                )
            )

            painter.setFont(
                QFont(
                    "Arial",
                    8,
                )
            )

            painter.drawText(
                button_rect,
                Qt.AlignCenter,
                text,
            )

            return button_rect

        self._force_control_rect = (
            draw_button(
                controls_x,
                72,
                "✓ FORCE_N"
                if self.force_visible
                else "FORCE_N",
                self.force_visible,
                self.force_color,
            )
        )

        self._raw_control_rect = (
            draw_button(
                controls_x + 76,
                55,
                "✓ RAW"
                if self.raw_visible
                else "RAW",
                self.raw_visible,
                self.raw_color,
            )
        )

        self._filtered_control_rect = (
            draw_button(
                controls_x + 135,
                80,
                "✓ FILTERED"
                if self.filtered_visible
                else "FILTERED",
                self.filtered_visible,
                self.filtered_color,
            )
        )

        # -------------------------------------------------
        # CYCLE ANALYSIS CONTROLS
        # -------------------------------------------------

        cycle_y = controls_y + self.control_height + 3

        def draw_cycle_button(x, width, text, checked, color):
            button_rect = QRectF(
                x,
                cycle_y,
                width,
                self.control_height,
            )

            painter.setBrush(
                QColor("#151B21") if checked
                else QColor("#0B0F14")
            )
            painter.setPen(
                QPen(
                    color if checked else self.axis_color,
                    1,
                )
            )
            painter.drawRoundedRect(
                button_rect, 3, 3
            )
            painter.setPen(
                QPen(
                    color if checked else self.text_color,
                    1,
                )
            )
            painter.setFont(QFont("Arial", 8))
            painter.drawText(
                button_rect,
                Qt.AlignCenter,
                text,
            )
            return button_rect

        cycle_x = int(plot.right()) - 220 - 5

        self._cycle_max_rect = draw_cycle_button(
            cycle_x, 50,
            "✓ MAX" if self.cycle_max_visible else "MAX",
            self.cycle_max_visible,
            self.cycle_max_color,
        )
        self._cycle_mid_rect = draw_cycle_button(
            cycle_x + 54, 50,
            "✓ MID" if self.cycle_mid_visible else "MID",
            self.cycle_mid_visible,
            self.cycle_mid_color,
        )
        self._cycle_min_rect = draw_cycle_button(
            cycle_x + 108, 50,
            "✓ MIN" if self.cycle_min_visible else "MIN",
            self.cycle_min_visible,
            self.cycle_min_color,
        )
        self._cycle_corridor_rect = draw_cycle_button(
            cycle_x + 162, 58,
            "✓ Δ" if self.cycle_corridor_visible else "Δ",
            self.cycle_corridor_visible,
            self.cycle_corridor_color,
        )

        # -------------------------------------------------
        # LIVE
        # -------------------------------------------------

        live_x = int(
            plot.left() + 20
        )

        live_y = int(
            plot.top() + 3
        )

        self._live_rect = QRectF(
            live_x,
            live_y,
            52,
            self.control_height,
        )

        painter.setBrush(
            QColor("#151B21")
            if self.live_mode
            else QColor("#0B0F14")
        )

        painter.setPen(
            QPen(
                self.force_color
                if self.live_mode
                else self.axis_color,
                1,
            )
        )

        painter.drawRoundedRect(
            self._live_rect,
            3,
            3,
        )

        painter.setPen(
            QPen(
                self.force_color
                if self.live_mode
                else self.text_color,
                1,
            )
        )

        painter.setFont(
            QFont(
                "Arial",
                8,
            )
        )

        painter.drawText(
            self._live_rect,
            Qt.AlignCenter,
            "● LIVE"
            if self.live_mode
            else "LIVE",
        )

        # -------------------------------------------------
        # X SCALE
        # -------------------------------------------------

        zoom_x = (
                live_x + 58
        )

        self._zoom_rect = QRectF(
            zoom_x,
            live_y,
            90,
            self.control_height,
        )

        painter.setBrush(
            QColor("#151B21")
        )

        painter.setPen(
            QPen(
                self.axis_color,
                1,
            )
        )

        painter.drawRoundedRect(
            self._zoom_rect,
            3,
            3,
        )

        painter.setPen(
            QPen(
                self.text_color
            )
        )

        painter.drawText(
            self._zoom_rect,
            Qt.AlignCenter,
            f"X {self.x_window_seconds:g} s",
        )

    # =====================================================
    # PAINT
    # =====================================================

    def paintEvent(
            self,
            event,
    ):

        painter = QPainter(
            self
        )

        painter.setRenderHint(
            QPainter.Antialiasing,
            True,
        )

        rect = self.rect()

        painter.fillRect(
            rect,
            self.background_color,
        )

        # -------------------------------------------------
        # Geometry
        # -------------------------------------------------

        left = 70
        right = 25
        top = 25
        bottom = 45

        plot = QRectF(
            left,
            top,
            max(
                10,
                rect.width()
                - left
                - right,
            ),
            max(
                10,
                rect.height()
                - top
                - bottom
                - self.scrollbar_height,
            ),
        )

        # -------------------------------------------------
        # Y SCALE
        # -------------------------------------------------

        if self.values:

            self.update_y_scale()

            force_min = 0.0
            force_max = self.y_max

        else:

            force_min = 0.0
            force_max = 1.0

        # -------------------------------------------------
        # GRID
        # -------------------------------------------------

        painter.setPen(
            QPen(
                self.grid_color,
                1,
            )
        )

        # -------------------------------------------------
        # Vertical grid
        # -------------------------------------------------

        for i in range(1, 10):
            x = (
                    plot.left()
                    + plot.width()
                    * i
                    / 10.0
            )

            painter.drawLine(
                int(x),
                int(plot.top()),
                int(x),
                int(plot.bottom()),
            )

        # -------------------------------------------------
        # Horizontal grid — EXACTLY 0.5 N
        # -------------------------------------------------

        value = 0.0

        while value <= force_max + 0.0001:
            ratio = (
                            force_max - value
                    ) / (
                            force_max - force_min
                    )

            y = (
                    plot.top()
                    + plot.height()
                    * ratio
            )

            painter.drawLine(
                int(plot.left()),
                int(y),
                int(plot.right()),
                int(y),
            )

            value += self.y_grid_step

        # -------------------------------------------------
        # Axes
        # -------------------------------------------------

        painter.setPen(
            QPen(
                self.axis_color,
                1,
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

        # -------------------------------------------------
        # Empty
        # -------------------------------------------------

        if (
                not self.values
                and not self.raw_values
                and not self.filtered_values
        ):

            painter.setPen(
                QPen(
                    self.text_color
                )
            )

            painter.setFont(
                QFont(
                    "Arial",
                    12,
                )
            )

            painter.drawText(
                plot,
                Qt.AlignCenter,
                "ОЖИДАНИЕ ИЗМЕРЕНИЯ",
            )

            # Y labels for empty graph

            value = 0.0

            painter.setFont(
                QFont(
                    "Arial",
                    9,
                )
            )

            while value <= force_max + 0.0001:
                ratio = (
                                force_max - value
                        ) / (
                                force_max - force_min
                        )

                y = (
                        plot.top()
                        + plot.height()
                        * ratio
                )

                painter.drawText(
                    5,
                    int(y - 8),
                    left - 12,
                    18,
                    Qt.AlignRight
                    | Qt.AlignVCenter,
                    f"{value:.1f}",
                )

                value += self.y_grid_step

            self.draw_controls(
                painter,
                plot,
            )

            self.draw_scrollbar(
                painter
            )

            return

        # -------------------------------------------------
        # Visible range
        # -------------------------------------------------

        start_index, end_index = (
            self.get_visible_range()
        )

        if end_index <= start_index:
            self.draw_controls(
                painter,
                plot,
            )

            self.draw_scrollbar(
                painter
            )

            return

        # -------------------------------------------------
        # Visible FORCE_N
        # -------------------------------------------------

        visible_force = self.values[
                        start_index:end_index
                        ]

        # -------------------------------------------------
        # Y LABELS
        # -------------------------------------------------

        if self.force_visible:

            painter.setPen(
                QPen(
                    self.text_color
                )
            )

            painter.setFont(
                QFont(
                    "Arial",
                    9,
                )
            )

            value = force_max

            while value >= force_min - 0.0001:
                ratio = (
                                force_max - value
                        ) / (
                                force_max - force_min
                        )

                y = (
                        plot.top()
                        + plot.height()
                        * ratio
                )

                painter.drawText(
                    5,
                    int(y - 8),
                    left - 12,
                    18,
                    Qt.AlignRight
                    | Qt.AlignVCenter,
                    f"{value:.1f}",
                )

                value -= self.y_grid_step

        # -------------------------------------------------
        # X LABELS
        # -------------------------------------------------

        visible_seconds = (
                len(visible_force)
                / self.sample_rate
        )

        if visible_seconds <= 0:
            visible_seconds = (
                self.x_window_seconds
            )

        painter.setPen(
            QPen(
                self.text_color
            )
        )

        painter.setFont(
            QFont(
                "Arial",
                9,
            )
        )

        painter.drawText(
            int(plot.left()),
            int(plot.bottom() + 8),
            120,
            20,
            Qt.AlignLeft
            | Qt.AlignVCenter,
            f"{start_index / self.sample_rate:.1f} s",
        )

        painter.drawText(
            int(plot.right() - 120),
            int(plot.bottom() + 8),
            120,
            20,
            Qt.AlignRight
            | Qt.AlignVCenter,
            f"{(start_index + len(visible_force)) / self.sample_rate:.1f} s",
        )

        # -------------------------------------------------
        # RAW
        # -------------------------------------------------

        if (
                self.raw_visible
                and len(self.raw_values)
                >= end_index
        ):
            visible_raw = self.raw_values[
                          start_index:end_index
                          ]

            raw_min, raw_max = (
                self.get_range(
                    visible_raw
                )
            )

            self.draw_signal(
                painter,
                plot,
                self.raw_values,
                self.raw_color,
                self.raw_style,
                raw_min,
                raw_max,
                1,
                start_index,
                end_index,
            )

        # -------------------------------------------------
        # FILTERED
        # -------------------------------------------------

        if (
                self.filtered_visible
                and len(self.filtered_values)
                >= end_index
        ):
            visible_filtered = (
                self.filtered_values[
                start_index:end_index
                ]
            )

            filtered_min, filtered_max = (
                self.get_range(
                    visible_filtered
                )
            )

            self.draw_signal(
                painter,
                plot,
                self.filtered_values,
                self.filtered_color,
                self.filtered_style,
                filtered_min,
                filtered_max,
                1,
                start_index,
                end_index,
            )

        # -------------------------------------------------
        # FORCE_N
        # -------------------------------------------------

        if (
                self.force_visible
                and len(self.values)
                >= end_index
        ):
            self.draw_signal(
                painter,
                plot,
                self.values,
                self.force_color,
                self.force_style,
                force_min,
                force_max,
                2,
                start_index,
                end_index,
            )

        # -------------------------------------------------
        # CYCLE ANALYSIS
        # -------------------------------------------------

        self.draw_cycle_analysis(
            painter,
            plot,
            force_min,
            force_max,
        )

        # -------------------------------------------------
        # Controls
        # -------------------------------------------------

        self.draw_controls(
            painter,
            plot,
        )

        self.draw_scrollbar(
            painter
        )

    # =====================================================
    # STYLE MENU
    # =====================================================

    def show_graph_menu(
            self,
            signal_name,
            global_pos,
    ):

        menu = QMenu(
            self
        )

        title = menu.addAction(
            signal_name
        )

        title.setEnabled(
            False
        )

        menu.addSeparator()

        # -------------------------------------------------
        # COLOR
        # -------------------------------------------------

        color_menu = menu.addMenu(
            "Цвет"
        )

        color_group = QActionGroup(
            color_menu
        )

        color_group.setExclusive(
            True
        )

        current_color = (
            self.get_signal_color(
                signal_name
            )
        )

        for (
                color_name,
                color_value
        ) in self.color_options.items():
            action = (
                color_menu.addAction(
                    color_name
                )
            )

            action.setCheckable(
                True
            )

            action.setChecked(
                QColor(
                    color_value
                ) == current_color
            )

            color_group.addAction(
                action
            )

            action.triggered.connect(
                lambda checked,
                       name=signal_name,
                       value=color_value:
                self.set_signal_color(
                    name,
                    value,
                )
            )

        # -------------------------------------------------
        # LINE STYLE
        # -------------------------------------------------

        style_menu = menu.addMenu(
            "Тип линии"
        )

        style_group = QActionGroup(
            style_menu
        )

        style_group.setExclusive(
            True
        )

        current_style = (
            self.get_signal_style(
                signal_name
            )
        )

        for (
                style_name,
                style_value
        ) in self.style_options.items():
            action = (
                style_menu.addAction(
                    style_name
                )
            )

            action.setCheckable(
                True
            )

            action.setChecked(
                style_value
                == current_style
            )

            style_group.addAction(
                action
            )

            action.triggered.connect(
                lambda checked,
                       name=signal_name,
                       value=style_value:
                self.set_signal_style(
                    name,
                    value,
                )
            )

        menu.exec_(
            global_pos
        )

    # =====================================================
    # COLORS
    # =====================================================

    def get_signal_color(
            self,
            signal_name,
    ):

        if signal_name == "FORCE_N":
            return self.force_color

        if signal_name == "RAW":
            return self.raw_color

        if signal_name == "FILTERED":
            return self.filtered_color

        if signal_name == "MAX":
            return self.cycle_max_color

        return QColor(
            "#FFFFFF"
        )

    # =====================================================

    def set_signal_color(
            self,
            signal_name,
            color_value,
    ):

        color = QColor(
            color_value
        )

        if signal_name == "FORCE_N":

            self.force_color = color

            if (
                    self.on_force_color_changed
                    is not None
            ):
                self.on_force_color_changed(
                    color
                )

        elif signal_name == "RAW":

            self.raw_color = color

        elif signal_name == "FILTERED":

            self.filtered_color = color

        self.update()

    # =====================================================
    # LINE STYLE
    # =====================================================

    def get_signal_style(
            self,
            signal_name,
    ):

        if signal_name == "FORCE_N":
            return self.force_style

        if signal_name == "RAW":
            return self.raw_style

        if signal_name == "FILTERED":
            return self.filtered_style

        return Qt.SolidLine

    # =====================================================

    def set_signal_style(
            self,
            signal_name,
            style,
    ):

        if signal_name == "FORCE_N":

            self.force_style = style

        elif signal_name == "RAW":

            self.raw_style = style

        elif signal_name == "FILTERED":

            self.filtered_style = style

        self.update()

    # =====================================================
    # MOUSE
    # =====================================================

    def mousePressEvent(
            self,
            event,
    ):

        pos = event.pos()

        # -------------------------------------------------
        # RIGHT CLICK
        # -------------------------------------------------

        if event.button() == Qt.RightButton:

            if self._force_control_rect.contains(
                    pos
            ):
                self.show_graph_menu(
                    "FORCE_N",
                    self.mapToGlobal(
                        pos
                    ),
                )

                return

            if self._raw_control_rect.contains(
                    pos
            ):
                self.show_graph_menu(
                    "RAW",
                    self.mapToGlobal(
                        pos
                    ),
                )

                return

            if self._filtered_control_rect.contains(
                    pos
            ):
                self.show_graph_menu(
                    "FILTERED",
                    self.mapToGlobal(
                        pos
                    ),
                )

                return

            if self._cycle_max_rect.contains(pos):
                self.show_graph_menu(
                    "MAX", self.mapToGlobal(pos)
                )
                return

            if self._cycle_mid_rect.contains(pos):
                self.show_graph_menu(
                    "MID", self.mapToGlobal(pos)
                )
                return

            if self._cycle_min_rect.contains(pos):
                self.show_graph_menu(
                    "MIN", self.mapToGlobal(pos)
                )
                return

            if self._cycle_corridor_rect.contains(pos):
                self.show_cycle_color_menu(
                    "CORRIDOR", self.mapToGlobal(pos)
                )
                return

        # -------------------------------------------------
        # LEFT CLICK
        # -------------------------------------------------

        if event.button() == Qt.LeftButton:

            if self._force_control_rect.contains(
                    pos
            ):
                self.set_force_visible(
                    not self.force_visible
                )

                return

            if self._raw_control_rect.contains(
                    pos
            ):
                self.set_raw_visible(
                    not self.raw_visible
                )

                return

            if self._filtered_control_rect.contains(
                    pos
            ):
                self.set_filtered_visible(
                    not self.filtered_visible
                )

                return

            if self._cycle_max_rect.contains(pos):
                self.set_cycle_max_visible(
                    not self.cycle_max_visible
                )
                return

            if self._cycle_mid_rect.contains(pos):
                self.set_cycle_mid_visible(
                    not self.cycle_mid_visible
                )
                return

            if self._cycle_min_rect.contains(pos):
                self.set_cycle_min_visible(
                    not self.cycle_min_visible
                )
                return

            if self._cycle_corridor_rect.contains(pos):
                self.set_cycle_corridor_visible(
                    not self.cycle_corridor_visible
                )
                return

            if self._live_rect.contains(
                    pos
            ):
                self.set_live_mode(
                    not self.live_mode
                )

                return

            # -------------------------------------------------
            # X SCALE
            # -------------------------------------------------

            if self._zoom_rect.contains(
                    pos
            ):
                self.show_x_scale_menu(
                    self.mapToGlobal(
                        pos
                    )
                )

                return

        # -------------------------------------------------
        # Scrollbar
        # -------------------------------------------------

        scrollbar = (
            self.get_scrollbar_geometry()
        )

        if (
                event.button()
                == Qt.LeftButton
                and scrollbar.contains(
            pos
        )
        ):
            self.handle_scrollbar_click(
                pos
            )

            return

        super().mousePressEvent(
            event
        )

    # =====================================================
    # MOUSE MOVE
    # =====================================================

    def mouseMoveEvent(
            self,
            event,
    ):

        if self._scroll_dragging:
            self.handle_scroll_drag(
                event.pos()
            )

            return

        super().mouseMoveEvent(
            event
        )

    # =====================================================
    # MOUSE RELEASE
    # =====================================================

    def mouseReleaseEvent(
            self,
            event,
    ):

        if (
                event.button()
                == Qt.LeftButton
        ):
            self._scroll_dragging = False

        super().mouseReleaseEvent(
            event
        )

    # =====================================================
    # SCROLLBAR CLICK
    # =====================================================

    def handle_scrollbar_click(
            self,
            pos,
    ):

        scrollbar = (
            self.get_scrollbar_geometry()
        )

        total = len(
            self.values
        )

        visible_count = (
            self.get_visible_count()
        )

        if total <= visible_count:
            return

        max_start = (
                total
                - visible_count
        )

        ratio = (
                        pos.x()
                        - scrollbar.left()
                ) / scrollbar.width()

        ratio = max(
            0.0,
            min(
                1.0,
                ratio,
            ),
        )

        self.view_start = int(
            ratio * max_start
        )

        self.live_mode = False

        self.update()

    # =====================================================
    # SCROLLBAR DRAG
    # =====================================================

    def handle_scroll_drag(
            self,
            pos,
    ):

        scrollbar = (
            self.get_scrollbar_geometry()
        )

        total = len(
            self.values
        )

        visible_count = (
            self.get_visible_count()
        )

        if total <= visible_count:
            return

        max_start = (
                total
                - visible_count
        )

        ratio = (
                        pos.x()
                        - scrollbar.left()
                ) / scrollbar.width()

        ratio = max(
            0.0,
            min(
                1.0,
                ratio,
            ),
        )

        self.view_start = int(
            ratio * max_start
        )

        self.live_mode = False

        self.update()

    # =====================================================
    # X SCALE MENU
    # =====================================================

    def show_x_scale_menu(
            self,
            global_pos,
    ):

        menu = QMenu(
            self
        )

        title = menu.addAction(
            "Ширина окна X"
        )

        title.setEnabled(
            False
        )

        menu.addSeparator()

        options = [
            0.5,
            1.0,
            2.0,
            5.0,
            10.0,
            30.0,
            60.0,
            120.0,
        ]

        group = QActionGroup(
            menu
        )

        group.setExclusive(
            True
        )

        for seconds in options:
            text = (
                f"{seconds:g} с"
            )

            action = menu.addAction(
                text
            )

            action.setCheckable(
                True
            )

            action.setChecked(
                abs(
                    seconds
                    - self.x_window_seconds
                )
                < 0.001
            )

            group.addAction(
                action
            )

            action.triggered.connect(
                lambda checked,
                       value=seconds:
                self.set_x_window(
                    value
                )
            )

        menu.exec_(
            global_pos
        )


# ============================================================
# MAIN WINDOW
# ============================================================

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

        self.cycle_delta_force_label = create_cycle_card(
            "ДЕЛЬТА",
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
