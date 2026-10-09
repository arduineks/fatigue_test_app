"""MeasurementSessionMixin — вынесен из calibration_window.py механическим
сплитом (байт-в-байт), ADR сплита монолита. Импорт-блок
исходного файла сохранён целиком; неиспользуемые импорты
безвредны и чистятся отдельно.
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



class MeasurementSessionMixin:
    # ========================================================
    # START
    # ========================================================

    def send_start(self):

        if (
                not self.connected
                or self.serial is None
        ):
            return

        # Новый старт измерения — перезарядить детекцию разрыва.
        self._rupture_handled = False

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

    @staticmethod
    def _set_text_if_changed(label, text):
        # setText на неизменившемся тексте вызывает переливейаут
        # и лишний repaint метки — а метки обновляются таймером
        # 100 мс. Сравнение с текущим текстом убирает эту работу.
        if label.text() != text:
            label.setText(text)

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

            self._set_text_if_changed(self.measurement_time_label,
                f"{elapsed:.3f} s"
            )

        # ----------------------------------------------------
        # Показатели, вычисляемые из входных данных.
        # Обновляются из последних принятых значений и
        # состояния анализа циклов в ForceGraphWidget.
        # ----------------------------------------------------

        if self.last_force_n is not None:
            self._set_text_if_changed(self.measurement_force_label,
                f"{self.convert_force_value(self.last_force_n):.3f} "
                f"{self.current_force_suffix()}"
            )

        if self.last_current_mm is not None:
            self._set_text_if_changed(self.traverse_position_label,
                f"{self.last_current_mm:.3f} mm"
            )

        # Актуальная скорость траверсы: слева стрелка
        # направления (зелёная), прочерк когда перемещения
        # нет (меньше порога шума квантования позиции).
        speed = self.measured_speed_mm_s

        if speed is None or abs(speed) <= 0.02:
            self._set_text_if_changed(self.traverse_speed_actual_label,
                "—"
            )
        else:
            arrow = "↑" if speed > 0 else "↓"

            self._set_text_if_changed(self.traverse_speed_actual_label,
                f"<span style='color: #39FF88;'>{arrow}</span> "
                f"{abs(speed):.3f}"
            )

        self._set_text_if_changed(self.measurement_frame_count_label,
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
            self._set_text_if_changed(self.measurement_freq_label,
                "— Hz"
            )
            self._set_text_if_changed(self.measurement_freq3_label,
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

            self._set_text_if_changed(self.measurement_freq_label,
                f"{freq_1s:.2f} Hz"
            )

            self._set_text_if_changed(self.measurement_freq3_label,
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

            self._set_text_if_changed(self.measurement_cpm_label,
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
            self._set_text_if_changed(self.cycle_min_force_label,
                f"{self.convert_force_value(stat_min):.2f} {suffix}"
            )

        if stat_max is not None:
            self._set_text_if_changed(self.cycle_max_force_label,
                f"{self.convert_force_value(stat_max):.2f} {suffix}"
            )

        if (
                stat_min is not None
                and stat_max is not None
        ):
            amplitude_force = (
                    stat_max
                    - stat_min
            ) / 2.0

            self._set_text_if_changed(self.cycle_amplitude_force_label,
                f"{self.convert_force_value(amplitude_force):.2f} {suffix}"
            )

        # Карточка СРЕДНЕЕ — MID всегда: свежие циклы — среднее
        # цикловых MIN/MAX; несвежие (или вне гейта) —
        # аппроксимация средним силы за последние 5 с (та же,
        # что линия MID на графике). Нет кадров за 5 с —
        # прежнее значение (среднее текущих экстремумов).
        mean_force = None

        if (
                self.measurement_running
                and graph.cycles_evaluating
                and stat_min is not None
                and stat_max is not None
                and graph.cycle_mid_fresh(3.0)
        ):
            mean_force = (
                    stat_min
                    + stat_max
            ) / 2.0
        else:
            mean_force = graph.recent_mid_n(5.0)

            if (
                    mean_force is None
                    and stat_min is not None
                    and stat_max is not None
            ):
                mean_force = (
                        stat_min
                        + stat_max
                ) / 2.0

        if mean_force is not None:
            self._set_text_if_changed(self.cycle_average_force_label,
                f"{self.convert_force_value(mean_force):.2f} {suffix}"
            )

        self._set_text_if_changed(self.cycle_count_label,
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
            "pos_mm": self.last_current_mm,
        })

    def last_known_position_mm(self):
        # Последняя известная позиция траверсы (мм): из истории
        # циклов, иначе — из последнего принятого кадра. None,
        # если данных нет.
        for row in reversed(self.session_recorder.history):
            pos = row.get("pos_mm")
            if pos is not None:
                return pos

        if self.last_current_mm is not None:
            return self.last_current_mm

        return None

    def elongation_base_mm(self):
        # База удлинения — позиция начала отсчёта удлинения:
        # нуль координаты траверсы в начале испытания. Позиция
        # траверсы в отчёте — абсолютная координата от нуля,
        # поэтому база удлинения = 0 (растяжение образца
        # отсчитывается от начала испытания).
        return 0.0

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
                # Новая запись — перезарядить детекцию разрыва.
                self._rupture_handled = False
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

        rows = [
            ("МИН", fmt(graph.cycle_min)),
            ("МАКС", fmt(graph.cycle_max)),
            ("СРЕДНЕЕ", fmt(graph.cycle_mid)),
            ("АМПЛИТУДА", fmt(graph.cycle_amplitude)),
            ("ЦИКЛЫ", str(graph.cycle_count)),
            ("ЧАСТОТА", self.measurement_freq_label.text()),
        ]

        # Позиция траверсы и удлинение — в мм как есть.
        # Удлинение = позиция траверсы в конце сессии − база
        # (нуль отсчёта удлинения): та же величина, что в карточке
        # «Положение траверсы, мм».
        last_pos = self.last_known_position_mm()
        base = self.elongation_base_mm()

        rows.append((
            "Позиция траверсы, мм",
            "—" if last_pos is None else f"{last_pos:.3f}",
        ))

        if base is not None and last_pos is not None:
            elongation = last_pos - base
            elong_text = f"{elongation:.3f}"
        else:
            elong_text = "—"

        rows.append(("Удлинение, мм", elong_text))

        # Параметры образца (настройки [RECORDING]) — в отчёт.
        rows.extend(self.specimen_info_rows())

        return rows

    def specimen_info_rows(self):
        # Строки параметров образца для отчёта (PDF/CSV): зажимная
        # длина, толщина, ширина, протокол испытания. Пустое поле
        # → «—».
        def value(edit):
            text = (edit.text() or "").strip()
            return text if text else "—"

        rows = []

        for label, attr in (
            ("Зажимная длина, мм", "specimen_grip_length_edit"),
            ("Толщина, мм", "specimen_thickness_edit"),
            ("Ширина, мм", "specimen_width_edit"),
            ("Протокол испытания", "specimen_protocol_edit"),
        ):
            edit = getattr(self, attr, None)

            if edit is not None:
                rows.append((label, value(edit)))

        return rows

    def build_recent_table_rows(self):
        # Поцикловые строки за последние 30 с для отчёта
        # (значения переведены в единицы отображения; позиция и
        # удлинение — в мм как есть).
        base = self.elongation_base_mm()
        rows = []

        protocol = ""
        protocol_edit = getattr(self, "specimen_protocol_edit", None)

        if protocol_edit is not None:
            protocol = (protocol_edit.text() or "").strip()

        for row in self.session_recorder.recent_rows(30.0):
            pos = row.get("pos_mm")

            pos_text = (
                f"{pos:.3f}"
                if pos is not None
                else ""
            )

            if base is not None and pos is not None:
                elong_text = f"{pos - base:.3f}"
            else:
                elong_text = ""

            rows.append((
                f"{row.get('elapsed_s', 0.0):.3f}",
                row.get("n", ""),
                f"{self.convert_force_value(row.get('max_n', 0.0)):.2f}",
                f"{self.convert_force_value(row.get('min_n', 0.0)):.2f}",
                f"{self.convert_force_value(row.get('mid_n', 0.0)):.2f}",
                f"{self.convert_force_value(row.get('amp_n', 0.0)):.2f}",
                pos_text,
                elong_text,
                protocol,
            ))

        return rows

    def build_graph_table_rows(self):
        # Данные графика (кадры) за последние 60 секунд для отчёта:
        # (время от старта, raw ADC, фильтрованный, сила в единицах
        # отображения, позиция мм).
        suffix = self.current_force_suffix()

        rows = []
        for t, raw, filtered, force_n, pos in (
                self.force_graph.get_recent_frames(60.0)
        ):
            pos_text = (
                f"{pos:.3f}"
                if pos is not None
                else ""
            )
            rows.append((
                f"{t:.3f}",
                f"{raw:.0f}",
                f"{filtered:.0f}",
                f"{self.convert_force_value(force_n):.3f} {suffix}",
                pos_text,
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

