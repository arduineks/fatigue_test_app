"""ForceGraphPG — вариант ForceGraphWidget на движке pyqtgraph
(шаг 2 миграции, ветка dev-pyqtgraph).

Состояние и логика — перенос без изменения семантики: буферы,
add_frame (FSM через CycleAnalyzerMixin, чанк-обрезка, адаптивный
SPS), видимые окна (live/X-window), недавние данные для записи,
MID-аппроксимация, свежесть пары MIN/MAX, FFT-фильтры отображения,
цвета/стили, целевые линии.

Отличия от legacy (ForceGraphWidget):
- рендер — pyqtgraph: 3 PlotDataItem с auto-downsampling (mode
  'peak' сохраняет пики) и clipToView; двойная буферизация/децимация
  reduceat/QPixmap-кэш НЕ нужны — это делает движок;
- линии SIG_MAX/SIG_M/SIG_MIN, MAX/MIN/MID и коридор цикла —
  InfiniteLine/LinearRegionItem, обновление — setPos/setData;
- кнопки управления — реальные QPushButtons-оверлей (те же ряды и
  семантика: ЛКМ — тумблер, ПКМ — меню цвета/FFT); кнопка
  «σ_M → цель» остаётся на вкладке «Измерение», как в legacy;
- история/прокрутка — штатное взаимодействие pyqtgraph
  (перетаскивание/колесо), мини-скроллбар legacy не воспроизводится;
- цифровые подписи правых шкал RAW/FILTERED — в следующей итерации
  (сами ряды нормируются на свой диапазон, как в legacy).

Выбор варианта — фабрика create_force_graph() в этом модуле:
legacy по умолчанию, pyqtgraph при USE_PYQTGRAPH_GRAPH.
"""

import time

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QPen
from PyQt5.QtWidgets import (
    QActionGroup,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.cycle_analyzer import CycleAnalyzerMixin
from src.fft_filter import FftDisplayFilter
from src.logging_setup import logger

pg.setConfigOptions(antialias=False, background="#0B0F14",
                    foreground="#8B9AA5")


class ForceGraphPG(QWidget, CycleAnalyzerMixin):
    """График силы на pyqtgraph. Совместим по состоянию с
    ForceGraphWidget (те же атрибуты/методы; поверхность API
    собрана grep'ом вызовов force_graph.* по src/)."""

    # Тот же порог осцилляции, что в legacy.
    RECENT_OSC_MIN_SPAN_N = 0.02

    # Колбэки смены цвета (присваиваются из MainWindow).
    on_force_color_changed = None
    on_mid_color_changed = None
    on_min_color_changed = None
    on_max_color_changed = None

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)

        # ---- Состояние (паритет с ForceGraphWidget.__init__) ----
        self.background_color = QColor("#0B0F14")
        self.sample_rate = 1000.0
        self.max_points = int(self.sample_rate * 120)
        self.trim_chunk = 512
        self.x_window_seconds = 5.0
        self.y_grid_step = 0.5

        self.values = []
        self.raw_values = []
        self.filtered_values = []
        self.frame_times = []
        self.frame_positions = []

        self.running = False
        self.start_time = None
        self.view_start = 0
        self.live_mode = True

        self.force_visible = True
        self.raw_visible = False
        self.filtered_visible = False

        self.force_color = QColor("#19E6FF")
        self.raw_color = QColor("#FFD400")
        self.filtered_color = QColor("#FF66CC")
        self.force_style = Qt.SolidLine
        self.raw_style = Qt.SolidLine
        self.filtered_style = Qt.SolidLine

        self.display_units = "N"
        self.area_mm2 = None

        self.raw_fft_filter = FftDisplayFilter(self.sample_rate)
        self.filtered_fft_filter = FftDisplayFilter(self.sample_rate)

        # Колбэки смены цвета (как в legacy).
        self.on_force_color_changed = None
        self.on_mid_color_changed = None
        self.on_min_color_changed = None
        self.on_max_color_changed = None

        # Гейтинг циклов (как в legacy).
        self.macro_move_speed = 0.05
        self._cycles_enabled = False
        self._macro_moving = False
        self.traverse_speed_mm_s = None
        self._force_stop_active = False
        self.external_cycles = False

        self.target_lines_visible = True
        self.target_sigma_max_n = None
        self.target_sigma_min_n = None
        self.target_sig_max_color = QColor("#FF6B6B")
        self.target_sig_mid_color = QColor("#FFD400")
        self.target_sig_min_color = QColor("#19E6FF")
        self.target_colors = {
            "SIG_MAX": self.target_sig_max_color,
            "SIG_M": self.target_sig_mid_color,
            "SIG_MIN": self.target_sig_min_color,
        }

        self._data_dirty = False
        self._srate_updated_at = 0.0

        # Цвета/видимость линий циклов (как в legacy __init__).
        self.cycle_max_color = QColor("#FF4D4D")
        self.cycle_mid_color = QColor("#FFFFFF")
        self.cycle_min_color = QColor("#4D9FFF")
        self.cycle_corridor_color = QColor("#19E6FF")
        self.cycle_max_visible = True
        self.cycle_mid_visible = True
        self.cycle_min_visible = True
        self.cycle_corridor_visible = True
        self._cycle_pair = None
        self._cycle_pair_t = None

        # FSM циклов — тот же миксин, что в legacy.
        self.cycle_tolerance = 0.03
        self.cycle_turn_confirm = 2
        self.reset_cycle_analysis()

        # ---- pyqtgraph ----
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot_item = pg.PlotWidget()
        self.plot = self.plot_item.getPlotItem()
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.hideAxis("bottom")
        self.plot.hideAxis("left")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setMenuEnabled(False)

        self._curve_force = self.plot.plot(
            pen=pg.mkPen(self.force_color, width=2),
            name="FORCE_N",
        )
        self._curve_raw = self.plot.plot(
            pen=pg.mkPen(self.raw_color, width=1),
            name="RAW",
        )
        self._curve_filtered = self.plot.plot(
            pen=pg.mkPen(self.filtered_color, width=1,
                         style=Qt.DashLine),
            name="FILTERED",
        )
        for curve in (self._curve_force, self._curve_raw,
                      self._curve_filtered):
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            curve.setSkipFiniteCheck(True)

        self._raw_y_span = (0.0, 1.0)
        self._filtered_y_span = (0.0, 1.0)

        # Целевые линии SIG_MAX / SIG_M / SIG_MIN.
        self._line_sig_max = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(self.target_sig_max_color, width=1,
                         style=Qt.DashLine),
        )
        self._line_sig_mid = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(self.target_sig_mid_color, width=1,
                         style=Qt.DotLine),
        )
        self._line_sig_min = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(self.target_sig_min_color, width=1,
                         style=Qt.DashLine),
        )
        for line in (self._line_sig_max, self._line_sig_mid,
                     self._line_sig_min):
            self.plot.addItem(line)

        # Линии циклов MAX / MID / MIN и коридор.
        self._line_cycle_max = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(self.cycle_max_color, width=1),
        )
        self._line_cycle_mid = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(self.cycle_mid_color, width=1,
                         style=Qt.DashDotLine),
        )
        self._line_cycle_min = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(self.cycle_min_color, width=1),
        )
        self._region_corridor = pg.LinearRegionItem(
            movable=False,
            brush=pg.mkBrush(25, 230, 255, 40),
        )
        self._region_corridor.setZValue(-10)
        self._region_corridor.lines[0].setPen(
            pg.mkPen(self.cycle_corridor_color, width=1)
        )
        self._region_corridor.lines[1].setPen(
            pg.mkPen(self.cycle_corridor_color, width=1)
        )
        for item in (self._line_cycle_max, self._line_cycle_mid,
                     self._line_cycle_min, self._region_corridor):
            self.plot.addItem(item)

        self.plot.setYRange(0.0, 1.0, padding=0)
        self.plot.disableAutoRange()

        layout.addWidget(self.plot_item)

        # ---- Кнопки управления (оверлей, те же ряды) ----
        self._buttons = {}
        self._build_control_buttons()

        # ---- Таймер обновления (тот же паттерн dirty) ----
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._refresh)
        self.update_timer.start(40)

    # =====================================================
    # КНОПКИ УПРАВЛЕНИЯ (оверлей; те же ряды, что в legacy)
    # =====================================================

    def _build_control_buttons(self):
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.setContentsMargins(6, 4, 6, 0)

        self._btn_force = self._make_button(
            "FORCE_N", "FORCE_N", self._toggle_force,
            self._menu_signal,
        )
        self._btn_raw = self._make_button(
            "RAW", "RAW", self._toggle_raw, self._menu_signal,
        )
        self._btn_filtered = self._make_button(
            "FILTERED", "FILTERED", self._toggle_filtered,
            self._menu_signal,
        )
        for b in (self._btn_force, self._btn_raw,
                  self._btn_filtered):
            row1.addWidget(b)
        row1.addStretch()

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        row2.setContentsMargins(6, 2, 6, 0)
        self._btn_max = self._make_button(
            "MAX", "MAX", self._toggle_max, self._menu_cycle,
        )
        self._btn_mid = self._make_button(
            "MID", "MID", self._toggle_mid, self._menu_cycle,
        )
        self._btn_min = self._make_button(
            "MIN", "MIN", self._toggle_min, self._menu_cycle,
        )
        self._btn_corr = self._make_button(
            "Δ", "CORRIDOR", self._toggle_corridor,
            self._menu_corridor,
        )
        for b in (self._btn_max, self._btn_mid, self._btn_min,
                  self._btn_corr):
            row2.addWidget(b)
        row2.addStretch()

        row3 = QHBoxLayout()
        row3.setSpacing(4)
        row3.setContentsMargins(6, 2, 6, 4)
        self._btn_sig_max = self._make_button(
            "SIG_MAX", "SIG_MAX", self._toggle_target_lines,
            self._menu_target,
        )
        self._btn_sig_mid = self._make_button(
            "SIG_M", "SIG_M", self._toggle_target_lines,
            self._menu_target,
        )
        self._btn_sig_min = self._make_button(
            "SIG_MIN", "SIG_MIN", self._toggle_target_lines,
            self._menu_target,
        )
        for b in (self._btn_sig_max, self._btn_sig_mid,
                  self._btn_sig_min):
            row3.addWidget(b)
        row3.addStretch()

        # Ряды кнопок — ПОД графиком (как нижние строки управления
        # в legacy, но в виде настоящих виджетов).
        for row in (row1, row2, row3):
            self.layout().addLayout(row)

        self._sync_button_labels()

    def _make_button(self, key, text, on_click, on_menu):
        b = QPushButton(text)
        b.setCheckable(False)
        b.setFixedHeight(22)
        b.setFixedWidth(max(52, 12 * len(text) + 10))
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(on_click)
        b.setContextMenuPolicy(Qt.CustomContextMenu)
        b.customContextMenuRequested.connect(
            lambda _pos, btn=b, k=key: on_menu(btn.mapToGlobal(_pos), k)
        )
        self._buttons[key] = b
        return b

    def _sync_button_labels(self):
        def mark(b, visible):
            text = b.text().lstrip("✓ ")
            b.setText(("✓ " + text) if visible else text)

        mark(self._btn_force, self.force_visible)
        mark(self._btn_raw, self.raw_visible)
        mark(self._btn_filtered, self.filtered_visible)
        mark(self._btn_max, self.cycle_max_visible)
        mark(self._btn_mid, self.cycle_mid_visible)
        mark(self._btn_min, self.cycle_min_visible)
        mark(self._btn_corr, self.cycle_corridor_visible)
        mark(self._btn_sig_max, self.target_lines_visible)
        mark(self._btn_sig_mid, self.target_lines_visible)
        mark(self._btn_sig_min, self.target_lines_visible)

    # --- обработчики кнопок (семантика legacy mousePress) ---

    def _toggle_force(self):
        self.set_force_visible(not self.force_visible)

    def _toggle_raw(self):
        self.set_raw_visible(not self.raw_visible)

    def _toggle_filtered(self):
        self.set_filtered_visible(not self.filtered_visible)

    def _toggle_max(self):
        self.set_cycle_max_visible(not self.cycle_max_visible)

    def _toggle_mid(self):
        self.set_cycle_mid_visible(not self.cycle_mid_visible)

    def _toggle_min(self):
        self.set_cycle_min_visible(not self.cycle_min_visible)

    def _toggle_corridor(self):
        self.set_cycle_corridor_visible(
            not self.cycle_corridor_visible
        )

    def _toggle_target_lines(self):
        self.set_target_lines_visible(not self.target_lines_visible)

    # --- меню (цвет / FFT) ---

    def _menu_signal(self, global_pos, signal_name):
        menu = QMenu(self)

        title = menu.addAction(signal_name)
        title.setEnabled(False)
        menu.addSeparator()

        color_menu = menu.addMenu("Цвет")
        group = QActionGroup(color_menu)
        for name, hexc in (
                ("Голубой", "#19E6FF"), ("Жёлтый", "#FFD400"),
                ("Красный", "#FF4D4D"), ("Зелёный", "#39FF88"),
                ("Белый", "#F2F5F7"), ("Оранжевый", "#FF9F43"),
                ("Фиолетовый", "#B388FF"), ("Розовый", "#FF66CC"),
        ):
            act = color_menu.addAction(name)
            act.setData(hexc)
            group.addAction(act)
        color_menu.triggered.connect(
            lambda act: self.set_signal_color(
                signal_name, QColor(act.data())
            )
        )

        if signal_name in ("RAW", "FILTERED"):
            menu.addSeparator()
            fft_menu = menu.addMenu("FFT-фильтр")
            fft_group = QActionGroup(fft_menu)
            modes = [
                ("Выключен", "off"),
                ("Low-pass 50 Гц", "lp50"),
                ("Notch 50 Гц", "notch50"),
            ]
            current = self.get_fft_mode(signal_name)
            for text, mode in modes:
                act = fft_menu.addAction(text)
                act.setCheckable(True)
                act.setChecked(mode == current)
                act.setData(mode)
                fft_group.addAction(act)
            fft_menu.triggered.connect(
                lambda act: self.set_fft_mode(
                    signal_name, act.data()
                )
            )

        menu.exec_(global_pos)

    def _menu_cycle(self, global_pos, name):
        menu = QMenu(self)
        title = menu.addAction(name)
        title.setEnabled(False)
        menu.addSeparator()
        color_menu = menu.addMenu("Цвет")
        group = QActionGroup(color_menu)
        for cname, hexc in (
                ("Красный", "#FF4D4D"), ("Белый", "#FFFFFF"),
                ("Голубой", "#4D9FFF"), ("Голубой яркий", "#19E6FF"),
        ):
            act = color_menu.addAction(cname)
            act.setData(hexc)
            group.addAction(act)
        color_menu.triggered.connect(
            lambda act: self.set_cycle_color(
                name, QColor(act.data())
            )
        )
        menu.exec_(global_pos)

    def _menu_corridor(self, global_pos, name):
        menu = QMenu(self)
        title = menu.addAction("Коридор цикла")
        title.setEnabled(False)
        menu.addSeparator()
        color_menu = menu.addMenu("Цвет")
        group = QActionGroup(color_menu)
        for cname, hexc in (
                ("Голубой яркий", "#19E6FF"), ("Голубой", "#4D9FFF"),
                ("Белый", "#F2F5F7"), ("Зелёный", "#39FF88"),
        ):
            act = color_menu.addAction(cname)
            act.setData(hexc)
            group.addAction(act)
        color_menu.triggered.connect(
            lambda act: self.set_cycle_color(
                name, QColor(act.data())
            )
        )
        menu.exec_(global_pos)

    def _menu_target(self, global_pos, name):
        menu = QMenu(self)
        title = menu.addAction(name)
        title.setEnabled(False)
        menu.addSeparator()
        color_menu = menu.addMenu("Цвет")
        group = QActionGroup(color_menu)
        for cname, hexc in (
                ("Красный", "#FF6B6B"), ("Жёлтый", "#FFD400"),
                ("Голубой яркий", "#19E6FF"), ("Белый", "#F2F5F7"),
                ("Зелёный", "#39FF88"),
        ):
            act = color_menu.addAction(cname)
            act.setData(hexc)
            group.addAction(act)
        color_menu.triggered.connect(
            lambda act: self.set_target_color(
                name, act.data()
            )
        )
        menu.exec_(global_pos)

    # =====================================================
    # ЕДИНИЦЫ / ПЛОЩАДЬ
    # =====================================================

    def to_display(self, force_n):
        if self.display_units == "MPa" and self.area_mm2:
            return force_n / self.area_mm2
        return force_n

    def unit_suffix(self):
        if self.display_units == "MPa" and self.area_mm2:
            return "MPa"
        return "N"

    def set_display_units(self, units):
        if units not in ("N", "MPa"):
            return
        self.display_units = units

    def set_area_mm2(self, area_mm2):
        self.area_mm2 = area_mm2

    # =====================================================
    # ДАННЫЕ
    # =====================================================

    def add_frame(self, raw, filtered, force_n, current_mm=None):
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

        if self.raw_fft_filter is not None:
            self.raw_fft_filter.append(raw)
        if self.filtered_fft_filter is not None:
            self.filtered_fft_filter.append(filtered)

        if getattr(self, "frame_positions", None) is None:
            self.frame_positions = []
        try:
            self.frame_positions.append(
                float(current_mm) if current_mm is not None else None
            )
        except (TypeError, ValueError):
            self.frame_positions.append(None)

        self._data_dirty = True

        # Адаптивный SPS (span) — как в legacy.
        now_s = self.frame_times[-1]
        n = len(self.frame_times)
        if (
                now_s - self._srate_updated_at > 2.0
                and n >= 64
        ):
            span = self.frame_times[-1] - self.frame_times[0]
            if span > 0:
                cand = (n - 1) / span
                if 100.0 <= cand <= 5000.0:
                    self.sample_rate = cand
                    self.max_points = int(cand * 120)
                    for _flt in (self.raw_fft_filter,
                                 self.filtered_fft_filter):
                        if _flt is not None:
                            _flt._default_fs = cand
                    self._srate_updated_at = now_s

        _skip_fsm = getattr(self, "external_cycles", False)

        if (
                not getattr(self, "_force_stop_active", False)
                and not _skip_fsm
        ):
            self.process_cycle(force_n)

        # Обрезка истории — чанком (как в legacy).
        if len(self.values) > self.max_points + self.trim_chunk:
            remove_count = len(self.values) - self.max_points
            del self.values[:remove_count]
            del self.raw_values[:remove_count]
            del self.filtered_values[:remove_count]
            if self.raw_fft_filter is not None:
                self.raw_fft_filter.trim_front(remove_count)
            if self.filtered_fft_filter is not None:
                self.filtered_fft_filter.trim_front(remove_count)
            del self.frame_times[:remove_count]
            del self.frame_positions[:remove_count]
            self.view_start = max(0, self.view_start - remove_count)

        if self.live_mode:
            self.view_start = self.get_live_start()

    # =====================================================
    # ВИДИМЫЕ ОКНА (паритет legacy)
    # =====================================================

    def get_visible_count(self):
        return max(
            2, int(self.x_window_seconds * self.sample_rate)
        )

    def get_live_start(self):
        return max(0, len(self.values) - self.get_visible_count())

    def get_visible_range(self):
        total = len(self.values)
        if total == 0:
            return 0, 0
        visible_count = self.get_visible_count()
        if self.live_mode:
            start = max(0, total - visible_count)
        else:
            max_start = max(0, total - visible_count)
            start = min(max(0, self.view_start), max_start)
        end = min(total, start + visible_count)
        return start, end

    def set_live_mode(self, enabled):
        self.live_mode = bool(enabled)
        if self.live_mode:
            self.view_start = self.get_live_start()
        self._data_dirty = True

    def set_x_window(self, seconds):
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return
        self.x_window_seconds = max(0.1, seconds)
        if self.live_mode:
            self.view_start = self.get_live_start()
        else:
            max_start = max(
                0, len(self.values) - self.get_visible_count()
            )
            self.view_start = min(self.view_start, max_start)
        self._data_dirty = True

    # =====================================================
    # ЖИЗНЕННЫЙ ЦИКЛ ИЗМЕРЕНИЯ
    # =====================================================

    def start_measurement(self):
        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()
        self.frame_times.clear()
        self.frame_positions.clear()
        if self.raw_fft_filter is not None:
            self.raw_fft_filter.reset()
        if self.filtered_fft_filter is not None:
            self.filtered_fft_filter.reset()

        self.running = True
        self.start_time = time.time()
        self.view_start = 0
        self.live_mode = True

        # Счётчик циклов переживает рестарт (сброс — «СБРОС»).
        preserved_count = self.cycle_count
        self.reset_cycle_analysis()
        self.cycle_count = preserved_count

        self.reset_y_scale()
        self._data_dirty = True

    def stop_measurement(self):
        self.running = False
        self._data_dirty = True

    def clear(self):
        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()
        self.frame_times.clear()
        self.frame_positions.clear()
        if self.raw_fft_filter is not None:
            self.raw_fft_filter.reset()
        if self.filtered_fft_filter is not None:
            self.filtered_fft_filter.reset()

        self.running = False
        self.start_time = None
        self.view_start = 0
        self.live_mode = True

        preserved_count = self.cycle_count
        self.reset_cycle_analysis()
        self.cycle_count = preserved_count

        self.reset_y_scale()
        self._data_dirty = True

    def reset_session(self):
        self.values.clear()
        self.raw_values.clear()
        self.filtered_values.clear()
        self.frame_times.clear()
        self.frame_positions.clear()
        if self.raw_fft_filter is not None:
            self.raw_fft_filter.reset()
        if self.filtered_fft_filter is not None:
            self.filtered_fft_filter.reset()

        self.view_start = 0
        self.live_mode = True
        self.reset_cycle_analysis()
        self.reset_y_scale()
        self._data_dirty = True

    def reset_y_scale(self):
        # В PG шкала Y пересчитывается на каждом refresh —
        # отдельного состояния не нужно; метод оставлен для
        # совместимости вызовов.
        self._data_dirty = True

    # =====================================================
    # ВИДИМОСТЬ / ЦВЕТА / СТИЛИ
    # =====================================================

    def set_force_visible(self, visible):
        self.force_visible = bool(visible)
        self._curve_force.setVisible(self.force_visible)
        self._sync_button_labels()

    def set_raw_visible(self, visible):
        self.raw_visible = bool(visible)
        self._curve_raw.setVisible(self.raw_visible)
        self._sync_button_labels()

    def set_filtered_visible(self, visible):
        self.filtered_visible = bool(visible)
        self._curve_filtered.setVisible(self.filtered_visible)
        self._sync_button_labels()

    def set_cycle_max_visible(self, visible):
        self.cycle_max_visible = bool(visible)
        self._line_cycle_max.setVisible(self.cycle_max_visible)
        self._sync_button_labels()

    def set_cycle_mid_visible(self, visible):
        self.cycle_mid_visible = bool(visible)
        self._line_cycle_mid.setVisible(self.cycle_mid_visible)
        self._sync_button_labels()

    def set_cycle_min_visible(self, visible):
        self.cycle_min_visible = bool(visible)
        self._line_cycle_min.setVisible(self.cycle_min_visible)
        self._sync_button_labels()

    def set_cycle_corridor_visible(self, visible):
        self.cycle_corridor_visible = bool(visible)
        self._region_corridor.setVisible(self.cycle_corridor_visible)
        self._sync_button_labels()

    def set_cycle_color(self, name, value):
        color = QColor(value)
        if name == "MAX":
            self.cycle_max_color = color
            self._line_cycle_max.setPen(
                pg.mkPen(color, width=1)
            )
        elif name == "MID":
            self.cycle_mid_color = color
            self._line_cycle_mid.setPen(
                pg.mkPen(color, width=1, style=Qt.DashDotLine)
            )
            if self.on_mid_color_changed is not None:
                self.on_mid_color_changed(color)
        elif name == "MIN":
            self.cycle_min_color = color
            self._line_cycle_min.setPen(
                pg.mkPen(color, width=1)
            )
            if self.on_min_color_changed is not None:
                self.on_min_color_changed(color)
        elif name == "CORRIDOR":
            self.cycle_corridor_color = color
            self._region_corridor.setBrush(
                pg.mkBrush(color.red(), color.green(), color.blue(),
                           40)
            )
            for line in self._region_corridor.lines:
                line.setPen(pg.mkPen(color, width=1))

    def set_target_color(self, name, color_name):
        color = QColor(color_name)
        self.target_colors[name] = color
        if name == "SIG_MAX":
            self.target_sig_max_color = color
            self._line_sig_max.setPen(
                pg.mkPen(color, width=1, style=Qt.DashLine)
            )
        elif name == "SIG_M":
            self.target_sig_mid_color = color
            self._line_sig_mid.setPen(
                pg.mkPen(color, width=1, style=Qt.DotLine)
            )
        elif name == "SIG_MIN":
            self.target_sig_min_color = color
            self._line_sig_min.setPen(
                pg.mkPen(color, width=1, style=Qt.DashLine)
            )

    def set_signal_color(self, signal_name, color_value):
        color = QColor(color_value)
        if signal_name == "FORCE_N":
            self.force_color = color
            self._curve_force.setPen(pg.mkPen(color, width=2))
            if self.on_force_color_changed is not None:
                self.on_force_color_changed(color)
        elif signal_name == "RAW":
            self.raw_color = color
            self._curve_raw.setPen(pg.mkPen(color, width=1))
        elif signal_name == "FILTERED":
            self.filtered_color = color
            self._curve_filtered.setPen(
                pg.mkPen(color, width=1, style=Qt.DashLine)
            )
        elif signal_name == "MID":
            self.set_cycle_color("MID", color)
        elif signal_name == "MIN":
            self.set_cycle_color("MIN", color)
        elif signal_name == "MAX":
            self.set_cycle_color("MAX", color)

    def get_signal_color(self, signal_name):
        if signal_name == "FORCE_N":
            return self.force_color
        if signal_name == "RAW":
            return self.raw_color
        if signal_name == "FILTERED":
            return self.filtered_color
        if signal_name == "MID":
            return self.cycle_mid_color
        if signal_name == "MIN":
            return self.cycle_min_color
        if signal_name == "MAX":
            return self.cycle_max_color
        return QColor("#FFFFFF")

    def get_signal_style(self, signal_name):
        if signal_name == "FORCE_N":
            return self.force_style
        if signal_name == "RAW":
            return self.raw_style
        if signal_name == "FILTERED":
            return self.filtered_style
        return Qt.SolidLine

    def set_signal_style(self, signal_name, style):
        if signal_name == "FORCE_N":
            self.force_style = style
            self._curve_force.setPen(
                pg.mkPen(self.force_color, width=2, style=style)
            )
        elif signal_name == "RAW":
            self.raw_style = style
            self._curve_raw.setPen(
                pg.mkPen(self.raw_color, width=1, style=style)
            )
        elif signal_name == "FILTERED":
            self.filtered_style = style
            self._curve_filtered.setPen(
                pg.mkPen(self.filtered_color, width=1, style=style)
            )

    # Обёртки, которых не хватало и в legacy (см. BUG: клик цвета
    # линии в главном меню падал с AttributeError).
    def set_force_color(self, color):
        self.set_signal_color("FORCE_N", color)

    def set_raw_color(self, color):
        self.set_signal_color("RAW", color)

    def set_filtered_color(self, color):
        self.set_signal_color("FILTERED", color)

    def set_force_style(self, style):
        self.set_signal_style("FORCE_N", style)

    def set_raw_style(self, style):
        self.set_signal_style("RAW", style)

    def set_filtered_style(self, style):
        self.set_signal_style("FILTERED", style)

    # =====================================================
    # ЦЕЛЕВЫЕ ЛИНИИ
    # =====================================================

    def set_target_lines(self, sigma_max_n=None, sigma_min_n=None):
        self.target_sigma_max_n = sigma_max_n
        self.target_sigma_min_n = sigma_min_n
        self._data_dirty = True

    def set_target_lines_visible(self, visible):
        self.target_lines_visible = bool(visible)
        self._sync_button_labels()
        self._data_dirty = True

    # =====================================================
    # FFT-ФИЛЬТРЫ ОТОБРАЖЕНИЯ
    # =====================================================

    def get_fft_mode(self, signal_name):
        f = self._fft_filter_for(signal_name)
        return f.mode if f is not None else "off"

    def set_fft_mode(self, signal_name, mode):
        f = self._fft_filter_for(signal_name)
        if f is None:
            return
        f.set_mode(mode, signal_name)
        self._data_dirty = True

    def _fft_filter_for(self, signal_name):
        if signal_name == "RAW":
            return self.raw_fft_filter
        if signal_name == "FILTERED":
            return self.filtered_fft_filter
        return None

    def _refresh_fft_filters(self):
        if self.raw_fft_filter is not None:
            try:
                self.raw_fft_filter.refresh(
                    self.raw_values, self.frame_times, "RAW"
                )
            except Exception:
                logger.exception("FFT-фильтр RAW: ошибка пересчёта")
        if self.filtered_fft_filter is not None:
            try:
                self.filtered_fft_filter.refresh(
                    self.filtered_values, self.frame_times,
                    "FILTERED",
                )
            except Exception:
                logger.exception(
                    "FFT-фильтр FILTERED: ошибка пересчёта"
                )

    def get_display_values(self, signal_name):
        if signal_name == "RAW":
            f = self.raw_fft_filter
        elif signal_name == "FILTERED":
            f = self.filtered_fft_filter
        else:
            return None
        if f is None or f.mode == "off":
            return None
        return f.filtered_values()

    # =====================================================
    # НЕДАВНИЕ ДАННЫЕ (паритет legacy)
    # =====================================================

    def get_recent_frames(self, seconds=60.0, absolute=False):
        if not self.values or not self.frame_times:
            return []
        now = time.time()
        cutoff = now - float(seconds)

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
                    if self.start_time is not None else t
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

    def recent_mid_n(self, seconds=5.0):
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
        index = 0
        while index < total and self.frame_times[index] < cutoff:
            index += 1

        recent = self.values[index:total]
        if not recent:
            return None

        win_min = min(recent)
        win_max = max(recent)
        if (win_max - win_min) >= self.RECENT_OSC_MIN_SPAN_N:
            return (win_max + win_min) / 2.0
        return sum(recent) / len(recent)

    def cycle_mid_fresh(self, seconds=3.0):
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
    # ОБНОВЛЕНИЕ ОТРИСОВКИ (таймер 40 мс + dirty)
    # =====================================================

    def _refresh(self):
        if not self._data_dirty:
            return
        self._data_dirty = False

        self._refresh_fft_filters()
        self._update_curves()
        self._update_lines()

    def _update_curves(self):
        start_index, end_index = self.get_visible_range()
        if end_index - start_index < 2:
            self._curve_force.setData([], [])
            self._curve_raw.setData([], [])
            self._curve_filtered.setData([], [])
            return

        t = self.frame_times[start_index:end_index]
        t0 = t[0]
        xs = [ti - t0 for ti in t]

        # FORCE_N — в единицах отображения, шкала Y по окну
        # (включая целевые линии) — как в legacy update_y_scale.
        force_n = np.asarray(
            self.values[start_index:end_index], dtype=float
        )
        if self.display_units == "MPa" and self.area_mm2:
            force_disp = force_n / self.area_mm2
        else:
            force_disp = force_n

        y_min, y_max = self._y_range_for(force_disp)
        self.plot.setYRange(y_min, y_max, padding=0)

        if self.force_visible:
            self._curve_force.setData(xs, force_disp, _callSync="off")

        # RAW/FILTERED — нормируются на СВОЙ диапазон окна
        # (как в legacy: собственный масштаб, не шкала силы).
        for curve, series, visible, span_attr in (
            (self._curve_raw, self.raw_values, self.raw_visible,
             "_raw_y_span"),
            (self._curve_filtered, self.filtered_values,
             self.filtered_visible, "_filtered_y_span"),
        ):
            if not visible:
                curve.setData([], [])
                continue
            arr = np.asarray(
                series[start_index:end_index], dtype=float
            )
            lo, hi = float(arr.min()), float(arr.max())
            if hi - lo <= 0:
                hi = lo + 1.0
            setattr(self, span_attr, (lo, hi))
            # Нормировка в 5–95 % высоты текущей шкалы Y силы.
            norm = 0.05 + 0.9 * (arr - lo) / (hi - lo)
            curve.setData(xs, y_min + norm * (y_max - y_min),
                          _callSync="off")

    def _y_range_for(self, force_disp):
        if force_disp.size == 0:
            return 0.0, 1.0
        lo = float(force_disp.min())
        hi = float(force_disp.max())

        # Целевые линии входят в шкалу (как в legacy).
        if self.target_lines_visible:
            targets = []
            if self.target_sigma_max_n is not None:
                targets.append(self.to_display(
                    self.target_sigma_max_n))
            if self.target_sigma_min_n is not None:
                targets.append(self.to_display(
                    self.target_sigma_min_n))
            if (self.target_sigma_max_n is not None
                    and self.target_sigma_min_n is not None):
                targets.append(self.to_display(
                    (self.target_sigma_max_n
                     + self.target_sigma_min_n) / 2.0))
            if targets:
                lo = min(lo, *targets)
                hi = max(hi, *targets)

        # Низ шкалы от нуля (как в legacy reset/update_y_scale),
        # шаг сетки 0.5 — округление вверх.
        if hi <= lo:
            hi = lo + 1.0
        step = self.y_grid_step if self.display_units == "N" else 0.05
        hi = (int(hi / step) + 1) * step
        return 0.0, hi

    def _update_lines(self):
        start_index, end_index = self.get_visible_range()
        visible = end_index - start_index >= 2
        if visible:
            t = self.frame_times[start_index:end_index]
            t0, t1 = t[0], t[-1]
        else:
            t0, t1 = 0.0, 1.0
        self._x_span = (t0, t1)
        self.plot.setXRange(0.0, max(0.1, t1 - t0), padding=0)

        # Целевые линии (в единицах отображения; X — время).
        for line, value_n in (
            (self._line_sig_max, self.target_sigma_max_n),
            (self._line_sig_mid,
             (self.target_sigma_max_n + self.target_sigma_min_n) / 2.0
             if (self.target_sigma_max_n is not None
                 and self.target_sigma_min_n is not None)
             else None),
            (self._line_sig_min, self.target_sigma_min_n),
        ):
            show = (
                    self.target_lines_visible
                    and value_n is not None
                    and visible
            )
            line.setVisible(show)
            if show:
                line.setPos(self.to_display(value_n))

        # Линии циклов.
        mid_fresh = self.cycle_mid_fresh(3.0)
        mid_value = (
            self.cycle_mid_display
            if (self.cycle_mid_display is not None and mid_fresh)
            else self.recent_mid_n(5.0)
            if self.recent_mid_n(5.0) is not None
            else self.cycle_mid
        )

        for line, value, vis in (
            (self._line_cycle_max, self.cycle_max,
             self.cycle_max_visible),
            (self._line_cycle_mid, mid_value,
             self.cycle_mid_visible),
            (self._line_cycle_min, self.cycle_min,
             self.cycle_min_visible),
        ):
            show = vis and value is not None and visible
            line.setVisible(show)
            if show:
                line.setPos(self.to_display(value))

        # Коридор цикла (MIN–MAX band).
        show = (
                self.cycle_corridor_visible
                and visible
                and self.cycle_max is not None
                and self.cycle_min is not None
        )
        self._region_corridor.setVisible(show)
        if show:
            self._region_corridor.setRegion(
                (self.to_display(self.cycle_min),
                 self.to_display(self.cycle_max))
            )


# =====================================================
# ФАБРИКА ГРАФИКА
# =====================================================

def create_force_graph(parent=None):
    """Фабрика графика: legacy ForceGraphWidget по умолчанию,
    pyqtgraph-вариант при USE_PYQTGRAPH_GRAPH. Поверхности API
    совместимы,MainWindow работает с обоими одинаково."""

    from src.protocol import USE_PYQTGRAPH_GRAPH

    if USE_PYQTGRAPH_GRAPH:
        from src.graph_pg import ForceGraphPG
        logger.info("GRAPH ENGINE: pyqtgraph")
        return ForceGraphPG(parent)

    from src.graph_widget import ForceGraphWidget
    logger.info("GRAPH ENGINE: legacy QPainter")
    return ForceGraphWidget(parent)
