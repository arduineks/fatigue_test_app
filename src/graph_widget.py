import math
import time

from PyQt5.QtCore import Qt, QTimer, QRectF, QPoint
from PyQt5.QtGui import QPainter, QPen, QFont, QColor
from PyQt5.QtWidgets import (
    QWidget,
    QMenu,
    QActionGroup, QColorDialog,
    QPushButton, QHBoxLayout,
)

from src.protocol import (
    GRAVITY,
)
from src.logging_setup import logger
from src.cycle_analyzer import CycleAnalyzerMixin

class ForceGraphWidget(CycleAnalyzerMixin, QWidget):

    def __init__(self, parent=None):

        super().__init__(parent)

        # =================================================
        # DATA
        # =================================================

        self.values = []
        self.raw_values = []
        self.filtered_values = []
        # Метки времени кадров (для экспорта данных графика окном).
        self.frame_times = []

        self.running = False
        self.start_time = None

        # Скорость траверсы (мм/с) — обновляется из окна
        # измерения; используется для гейтинга циклов.
        self.traverse_speed_mm_s = None
        self._macro_moving = False

        # Порог начала оценки циклов: циклы считаются только
        # когда управляемая величина в допуске от цели
        # (±0.1 Н / ±0.2 МПа), задаётся извне.
        self._cycles_enabled = True

        # Порог «макро-перемещения» траверсы: быстрее — циклы
        # не считаются (показания нестабильны), медленнее
        # (подстройки регулятора во время осцилляции) — считаются.
        self.macro_move_speed = 0.05

        # Максимальная история.
        # 330 SPS × 120 с — в памяти держим только окно 120 с.
        self.max_points = 330 * 120

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
        self.on_mid_color_changed = None
        self.on_min_color_changed = None
        self.on_max_color_changed = None

        # =================================================
        # DISPLAY UNITS
        # =================================================
        # Внутри всё хранится в Н; МПа — только конверсия
        # при отрисовке: значение / площадь сечения (мм2).
        # "N" или "MPa".
        self.display_units = "N"
        self.area_mm2 = None
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

        # Текущий диапазон шкалы Y (в единицах отображения).
        # Автоподстройка: верх = наименьшее целое >= максимума
        # данных (min(округление вверх, округление вниз + 1)),
        # низ = наибольшее целое <= минимума данных. Шкала
        # подстраивается в обе стороны по текущему окну.
        self.y_min = 0.0
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

        # Слайдбар истории убран: график показывает только
        # текущее окно, память ограничена max_points.
        self.scrollbar_height = 0

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

        self.cycle_pending_max = None
        self.cycle_pending_min = None

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
        self._target_max_rect = QRectF()
        self._target_mid_rect = QRectF()
        self._target_min_rect = QRectF()

        # =================================================
        # TARGET LINES (цель цикла: σ_max, σ_min)
        # =================================================
        # Стационарные горизонтальные линии цели качества
        # цикла: SIG_MAX / SIG_M / SIG_MIN. Значения хранятся
        # в Н; при отрисовке конвертируются в единицы шкалы
        # через to_display().
        self.target_sigma_max_n = None
        self.target_sigma_min_n = None
        self.target_lines_visible = True

        self.target_sig_max_color = QColor("#FF6B6B")
        self.target_sig_mid_color = QColor("#FFD400")
        self.target_sig_min_color = QColor("#19E6FF")

        # Цвета целевых линий — редактируемые (меню цвета из
        # оверлея кнопок стиля). Источник истины — dict; три
        # атрибута выше оставлены как алиасы совместимости.
        self.target_colors = {
            "SIG_MAX": self.target_sig_max_color,
            "SIG_M": self.target_sig_mid_color,
            "SIG_MIN": self.target_sig_min_color,
        }

        # Полное состояние циклов — через reset_cycle_analysis
        # (FSM теперь работает с первого кадра, до любого
        # start/reset; вручную выше cycle_mid_display пропущен —
        # BUG после сплита FSM-всегда).
        self.reset_cycle_analysis()

        # (Кнопки стиля кривых рисуются в ряду управления графика —
        # draw_cycle_button, стиль как у MAX/MID/MIN.)

    # =====================================================
    # Y SCALE
    # =====================================================

    def set_display_units(self, units):

        if units not in ("N", "MPa"):
            return

        self.display_units = units

        # Масштаб Y считается в единицах отображения:
        # при смене единиц пересчитать заново.
        self.y_max = 1.0
        self.update_y_scale()

        self.update()

    def set_area_mm2(self, area_mm2):

        try:
            area_mm2 = float(area_mm2)
        except (TypeError, ValueError):
            area_mm2 = None

        if area_mm2 is not None and area_mm2 > 0:
            self.area_mm2 = area_mm2
        else:
            self.area_mm2 = None

        # Масштаб Y в единицах отображения: пересчитать.
        self.y_max = 1.0
        self.update_y_scale()

        self.update()

    def to_display(self, force_n):
        # Н → единицы отображения. В МПа, если известна
        # площадь сечения; иначе возвращаем как есть (Н).
        if (
                self.display_units == "MPa"
                and self.area_mm2
        ):
            return (
                    force_n
                    / self.area_mm2
            )

        return force_n

    def unit_suffix(self):

        if (
                self.display_units == "MPa"
                and self.area_mm2
        ):
            return "MPa"

        return "N"

    def reset_y_scale(self):

        self.y_min = 0.0
        self.y_max = 1.0

    def update_y_scale(self):

        if not self.values:
            self.y_min = 0.0
            self.y_max = 1.0
            return

        # Источник данных — ВИДИМОЕ окно (тот же диапазон, что
        # рисует paintEvent), а не весь буфер 120 с: шкала
        # подстраивается по тому, что реально на экране.
        start_index, end_index = self.get_visible_range()
        visible_values = self.values[start_index:end_index]

        if not visible_values:
            self.y_min = 0.0
            self.y_max = 1.0
            return

        # Границы данных (в единицах отображения: Н или МПа).
        data_max = self.to_display(
            max(visible_values)
        )
        data_min = self.to_display(
            min(visible_values)
        )

        # Целевые линии (SIG_MAX / SIG_M / SIG_MIN) входят в
        # расчёт вертикальной шкалы: шкала охватывает и данные,
        # и цели. Линии невидимы или значения не заданы —
        # не влияют.
        if self.target_lines_visible:

            target_values_n = []

            if self.target_sigma_max_n is not None:
                target_values_n.append(
                    self.target_sigma_max_n
                )

            if self.target_sigma_min_n is not None:
                target_values_n.append(
                    self.target_sigma_min_n
                )

            if (
                    self.target_sigma_max_n is not None
                    and self.target_sigma_min_n is not None
            ):
                target_values_n.append(
                    (
                            self.target_sigma_max_n
                            + self.target_sigma_min_n
                    ) / 2.0
                )

            for target_n in target_values_n:
                target_display = self.to_display(
                    target_n
                )
                data_max = max(data_max, target_display)
                data_min = min(data_min, target_display)

        # Сила ниже нуля не должна уводить верх шкалы
        # в минус при полностью отрицательном сигнале.
        data_max = max(0.0, data_max)

        # Верх шкалы: наименьшее целое >= максимума данных —
        # min(округление вверх до целого, округление вниз до
        # целого + 1); целое значение остаётся без запаса.
        required_max = min(
            math.ceil(data_max),
            math.floor(data_max) + 1,
        )

        # Низ шкалы: по умолчанию ось начинается с нуля; в минус
        # уходим только когда данные уходят в минус (авторасширение).
        if data_min < 0:
            # Наибольшее целое <= минимума данных —
            # max(округление вниз до целого, округление вверх
            # до целого - 1).
            required_min = max(
                math.floor(data_min),
                math.ceil(data_min) - 1,
            )
        else:
            required_min = 0.0

        # Минимальный диапазон шкалы.
        if required_max - required_min < 1.0:
            required_max = required_min + 1.0

        self.y_min = required_min
        self.y_max = required_max

    # =====================================================
    # MEASUREMENT
    # =====================================================

    def start_measurement(self):

        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()
        self.frame_times.clear()
        self.frame_positions = []

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
        self.frame_times.clear()
        self.frame_positions = []

        self.running = False
        self.start_time = None

        self.view_start = 0
        self.live_mode = True

        self.reset_cycle_analysis()

        self.reset_y_scale()

        self.update()

    # =====================================================

    def reset_session(self):
        # Сброс сессии (кнопка «СБРОС»): обнуляет график,
        # счётчик циклов и статистику циклов. Состояние записи
        # (device running) не трогаем — только данные сессии.
        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()
        self.frame_times.clear()
        self.frame_positions = []

        self.view_start = 0
        self.live_mode = True

        self.reset_cycle_analysis()

        self.reset_y_scale()

        self.update()

    def get_recent_frames(
            self,
            seconds=60.0,
            absolute=False,
    ):
        # Данные графика за последние `seconds` секунд:
        # список (t, raw, filtered, force_n, current_mm).
        # absolute=False → t = секунды от начала измерения;
        # absolute=True → t = unix-время кадра (для записи в CSV).
        if not self.values or not self.frame_times:
            return []

        now = time.time()
        cutoff = now - float(seconds)

        # Первый кадр, попавший в окно (times возрастают).
        index = 0
        total = min(len(self.frame_times), len(self.values))
        while index < total and self.frame_times[index] < cutoff:
            index += 1

        positions = getattr(self, "frame_positions", None) or []

        rows = []
        for i in range(index, total):
            t = self.frame_times[i]
            if absolute:
                stamp = t
            else:
                stamp = (
                    t - self.start_time
                    if self.start_time is not None
                    else t
                )
                stamp = max(0.0, stamp)
            pos = positions[i] if i < len(positions) else None
            rows.append((
                stamp,
                self.raw_values[i],
                self.filtered_values[i],
                self.values[i],
                pos,
            ))

        return rows

    def recent_mean_n(self, seconds=5.0):
        # Среднее силы (Н) по кадрам за последние `seconds`
        # секунд. Источник — существующие буферы values /
        # frame_times (параллельные списки), ничего не
        # дублируется. None, если данных нет.
        if not self.values or not self.frame_times:
            return None

        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return None

        if seconds <= 0:
            return None

        cutoff = time.time() - seconds

        total = min(len(self.values), len(self.frame_times))

        # Первый кадр в окне (frame_times возрастают).
        index = 0
        while index < total and self.frame_times[index] < cutoff:
            index += 1

        recent = self.values[index:total]
        if not recent:
            return None

        return sum(recent) / len(recent)

    def cycle_mid_fresh(self, seconds=3.0):
        # Свежесть циклового MID по паре (cycle_min, cycle_max):
        # метка времени обновляется при СМЕНЕ пары. Циклы
        # считаются свежими, если пара обновилась не позже
        # `seconds` назад (аналог анти-фриза регулятора,
        # MAINTAIN_CTRL_FREEZE_S). Используется ТОЛЬКО
        # отображением/подстройкой; детект циклов не трогает.
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 3.0

        pair = (self.cycle_min, self.cycle_max)

        if pair != getattr(self, "_cycle_pair", None):
            self._cycle_pair = pair
            self._cycle_pair_t = time.time()

        pair_t = getattr(self, "_cycle_pair_t", None)
        if pair_t is None:
            return False

        return (time.time() - pair_t) <= seconds

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
            current_mm=None,
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
        self.frame_times.append(time.time())

        # Позиция траверсы из кадра — для петли гистерезиса
        # и экспорта кадров (хранится параллельно кадрам).
        if getattr(self, "frame_positions", None) is None:
            self.frame_positions = []
        try:
            self.frame_positions.append(
                float(current_mm)
                if current_mm is not None
                else None
            )
        except (TypeError, ValueError):
            self.frame_positions.append(None)

        # FSM и счёт частоты — ВСЕГДА по кадрам, КРОМЕ активного стопора
        # по силе: при прижиме к концевику детектор считает мусорные
        # «циклы» (MID уходит в десятки отрицательных Н) — на время
        # аварии подсчёт подавлен (флаг ставит MainWindow).
        if not getattr(self, "_force_stop_active", False):
            self.process_cycle(force_n)

        # -------------------------------------------------
        # Y scale пересчитывается в update_y_scale() при
        # отрисовке (paint → _refresh_graph): границы окна
        # данных, автоподстройка в обе стороны.
        #
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

            del self.frame_times[
                :remove_count
                ]

            del self.frame_positions[
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

            if (
                    self.on_max_color_changed
                    is not None
            ):
                self.on_max_color_changed(
                    color
                )
        elif name == "MID":
            self.cycle_mid_color = color

            if (
                    self.on_mid_color_changed
                    is not None
            ):
                self.on_mid_color_changed(
                    color
                )
        elif name == "MIN":
            self.cycle_min_color = color

            if (
                    self.on_min_color_changed
                    is not None
            ):
                self.on_min_color_changed(
                    color
                )
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
    # STYLE OVERLAY (кнопки стиля кривых, правый верхний угол)
    # =====================================================

    # =====================================================
    # TARGET LINE COLORS (меню цвета целевых линий)
    # =====================================================

    def get_target_color(self, name):
        return self.target_colors.get(
            name,
            QColor("#FFFFFF"),
        )

    def set_target_color(self, name, color_name):
        # Цвет целевой линии (SIG_MAX / SIG_M / SIG_MIN).
        # В ini не сохраняется — дефолт в сессии.
        if name not in self.target_colors:
            return

        color = QColor(color_name)
        if not color.isValid():
            return

        self.target_colors[name] = color

        if name == "SIG_MAX":
            self.target_sig_max_color = color
        elif name == "SIG_M":
            self.target_sig_mid_color = color
        elif name == "SIG_MIN":
            self.target_sig_min_color = color

        self.update()

    def show_target_color_menu(self, name, global_pos):
        color = QColorDialog.getColor(
            self.get_target_color(name),
            self,
            f"Цвет {name}",
        )
        if color.isValid():
            self.set_target_color(name, color.name())

        # =====================================================
        # DRAW CYCLE ANALYSIS
        # =====================================================

    def draw_cycle_analysis(self, painter, plot, force_min, force_max):
        # MID рисуется ВСЕГДА:
        # - свежий цикловой MID (циклы обновлялись не позже 3 с
        #   назад) — как раньше: EMA (cycle_mid_display),
        #   DashDotLine, цвет MID;
        # - при несвежих циклах — горизонтальная аппроксимация
        #   на уровне среднего силы за последние 5 с
        #   (recent_mean_n), пунктир и приглушённый цвет, чтобы
        #   оператор видел отличие от циклового MID.
        # MAX/MIN/коридор — как раньше, только когда есть
        # цикловые значения.

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

        has_cycle = (
                self.cycle_max is not None
                and self.cycle_min is not None
        )

        if has_cycle:

            y_max = y_for(
                self.to_display(self.cycle_max)
            )

            y_min = y_for(
                self.to_display(self.cycle_min)
            )

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

        if not self.cycle_mid_visible:
            return

        # MID показываем всегда. Свежий цикловой MID берём
        # только если циклы реально свежи и EMA посчитана.
        fresh = (
                has_cycle
                and self.cycle_mid_fresh(3.0)
        )

        if (
                fresh
                and self.cycle_mid_display is not None
        ):
            mid_value = self.cycle_mid_display
            mid_pen = QPen(
                self.cycle_mid_color,
                1.5,
                Qt.DashDotLine,
            )
        else:
            # Аппроксимация: горизонталь на уровне среднего
            # силы за последние 5 с пришедших кадров.
            mid_value = self.recent_mean_n(5.0)
            if mid_value is None:
                return
            approx_color = QColor(self.cycle_mid_color)
            approx_color.setAlpha(150)
            mid_pen = QPen(
                approx_color,
                1.2,
                Qt.DashLine,
            )

        y_mid_display = y_for(
            self.to_display(mid_value)
        )

        painter.setPen(mid_pen)
        painter.drawLine(
            int(plot.left()), int(y_mid_display),
            int(plot.right()), int(y_mid_display),
        )

    # =====================================================
    # TARGET LINES (цель цикла)
    # =====================================================

    def set_target_lines(self, sigma_max_n=None, sigma_min_n=None):
        # Целевые значения цикла (Н). None — линия/пара линий
        # не задана и не рисуется. Обе линии задаются вместе;
        # sigma_min_n обычно = R × sigma_max_n.
        try:
            sigma_max_n = (
                None
                if sigma_max_n is None
                else float(sigma_max_n)
            )
        except (TypeError, ValueError):
            sigma_max_n = None

        try:
            sigma_min_n = (
                None
                if sigma_min_n is None
                else float(sigma_min_n)
            )
        except (TypeError, ValueError):
            sigma_min_n = None

        self.target_sigma_max_n = sigma_max_n
        self.target_sigma_min_n = sigma_min_n

        self.update()

    def set_target_lines_visible(self, visible):
        # Показ целевых линий на графике (тумблер).
        self.target_lines_visible = bool(visible)
        self.update()

    def draw_target_lines(self, painter, plot, force_min, force_max):
        # Три стационарные горизонтальные линии цели:
        # SIG_MAX (#FF6B6B), SIG_M — середина σ_max/σ_min
        # (#FFD400), SIG_MIN (#19E6FF), все пунктиром.
        # Значения конвертируются в единицы шкалы через
        # to_display(); линия рисуется только внутри plot и
        # только если попадает в диапазон шкалы.
        if not self.target_lines_visible:
            return

        if (
                self.target_sigma_max_n is None
                and self.target_sigma_min_n is None
        ):
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

        entries = []

        if self.target_sigma_max_n is not None:
            entries.append(
                (
                    self.to_display(self.target_sigma_max_n),
                    self.target_colors["SIG_MAX"],
                    "SIG_MAX",
                )
            )

        if (
                self.target_sigma_max_n is not None
                and self.target_sigma_min_n is not None
        ):
            entries.append(
                (
                    self.to_display(
                        (
                                self.target_sigma_max_n
                                + self.target_sigma_min_n
                        ) / 2.0
                    ),
                    self.target_colors["SIG_M"],
                    "SIG_M",
                )
            )

        if self.target_sigma_min_n is not None:
            entries.append(
                (
                    self.to_display(self.target_sigma_min_n),
                    self.target_colors["SIG_MIN"],
                    "SIG_MIN",
                )
            )

        # Подпись — мелким шрифтом у правого края линии
        # (по образцу мелких подписей кнопок цикла).
        painter.setFont(QFont("Arial", 8))

        for value, color, label in entries:

            if value < force_min or value > force_max:
                continue

            y = y_for(value)

            painter.setPen(
                QPen(
                    color,
                    1.2,
                    Qt.DashLine,
                )
            )

            painter.drawLine(
                int(plot.left()), int(y),
                int(plot.right()), int(y),
            )

            painter.setPen(
                QPen(color)
            )

            painter.drawText(
                int(plot.right() - 66),
                int(y - 13),
                60,
                11,
                Qt.AlignRight | Qt.AlignVCenter,
                label,
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
        # TARGET LINE CONTROLS — ТРЕТИЙ РЯД (тот же стиль
        # draw_cycle_button)
        # -------------------------------------------------

        target_y = cycle_y + self.control_height + 3

        def draw_target_button(x, width, text, checked, color):
            button_rect = QRectF(
                x,
                target_y,
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

        self._target_max_rect = draw_target_button(
            cycle_x, 58,
            "✓ SIG_MAX" if self.target_lines_visible else "SIG_MAX",
            self.target_lines_visible,
            self.target_colors["SIG_MAX"],
        )
        self._target_mid_rect = draw_target_button(
            cycle_x + 62, 52,
            "✓ SIG_M" if self.target_lines_visible else "SIG_M",
            self.target_lines_visible,
            self.target_colors["SIG_M"],
        )
        self._target_min_rect = draw_target_button(
            cycle_x + 118, 58,
            "✓ SIG_MIN" if self.target_lines_visible else "SIG_MIN",
            self.target_lines_visible,
            self.target_colors["SIG_MIN"],
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

            force_min = self.y_min
            force_max = self.y_max

        else:

            force_min = 0.0
            force_max = 1.0

        # Единицы отображения для подписи значений.
        unit_suffix = self.unit_suffix()

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

        value = force_min

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
                    f"{value:.1f} {unit_suffix}",
                )

                value += self.y_grid_step

            self.draw_controls(
                painter,
                plot,
            )

            if self.scrollbar_height > 0:
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

            if self.scrollbar_height > 0:
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

        # В единицах отображения (Н или МПа).
        visible_force = [
            self.to_display(v)
            for v in visible_force
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
                    f"{value:.1f} {unit_suffix}",
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
                visible_force,
                self.force_color,
                self.force_style,
                force_min,
                force_max,
                2,
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
        # TARGET LINES (цель цикла)
        # -------------------------------------------------

        self.draw_target_lines(
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

        if self.scrollbar_height > 0:
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

        elif signal_name == "MID":

            self.cycle_mid_color = color

            if (
                    self.on_mid_color_changed
                    is not None
            ):
                self.on_mid_color_changed(
                    color
                )

        elif signal_name == "MIN":

            self.cycle_min_color = color

            if (
                    self.on_min_color_changed
                    is not None
            ):
                self.on_min_color_changed(
                    color
                )

        elif signal_name == "MAX":

            self.cycle_max_color = color

            if (
                    self.on_max_color_changed
                    is not None
            ):
                self.on_max_color_changed(
                    color
                )

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

            if self._target_max_rect.contains(pos):
                self.show_target_color_menu(
                    "SIG_MAX", self.mapToGlobal(pos)
                )
                return

            if self._target_mid_rect.contains(pos):
                self.show_target_color_menu(
                    "SIG_M", self.mapToGlobal(pos)
                )
                return

            if self._target_min_rect.contains(pos):
                self.show_target_color_menu(
                    "SIG_MIN", self.mapToGlobal(pos)
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

            if self._target_max_rect.contains(pos):
                self.set_target_lines_visible(
                    not self.target_lines_visible
                )
                return

            if self._target_mid_rect.contains(pos):
                self.set_target_lines_visible(
                    not self.target_lines_visible
                )
                return

            if self._target_min_rect.contains(pos):
                self.set_target_lines_visible(
                    not self.target_lines_visible
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
                and self.scrollbar_height > 0
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
