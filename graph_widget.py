import math
import time

from PyQt5.QtCore import Qt, QTimer, QRectF
from PyQt5.QtGui import QPainter, QPen, QFont, QColor
from PyQt5.QtWidgets import (
    QWidget,
    QMenu,
    QActionGroup, QColorDialog,
)

from protocol import (
    GRAVITY,
)

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

        # Средняя линия (белая) строится по N последним
        # завершённым циклам: среднее их mid-значений.
        self.cycle_mid_history = []
        self.cycle_mid_history_len = 5

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

        self.cycle_mid_history = []

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

                # Средняя линия — среднее mid-значений
                # последних N завершённых циклов.
                self.cycle_mid_history.append(
                    self.cycle_mid_value
                )

                if (
                        len(self.cycle_mid_history)
                        > self.cycle_mid_history_len
                ):
                    del self.cycle_mid_history[
                        :len(self.cycle_mid_history)
                        - self.cycle_mid_history_len
                    ]

                self.cycle_mid = (
                        sum(self.cycle_mid_history)
                        / len(self.cycle_mid_history)
                )

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
