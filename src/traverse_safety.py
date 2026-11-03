"""TraverseSafetyMixin — вынесен из calibration_window.py механическим
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
from src.home_dialog import HomeCalibrationDialog



class TraverseSafetyMixin:
    # ========================================================
    # ДЕТЕКЦИЯ РАЗРЫВА ОБРАЗЦА (константы, переопределяются
    # секцией [RUPTURE] app_settings.ini — см. app_settings_io)
    # ========================================================

    # Около-нулевое значение силы, Н: ниже него разрыв.
    RUPTURE_NEAR_ZERO_N = 0.05
    # Окно истории для оценки пика силы, с.
    RUPTURE_WINDOW_S = 5.0
    # Сессия считается нагруженной, если пик силы за окно >= этого.
    RUPTURE_MIN_PEAK_N = 0.30
    # Около-ноль должен продержаться столько, чтобы не сработать
    # на шуме или коротком провале, с.
    RUPTURE_HOLD_S = 1.0
    # Период переоценки разрыва, с. check_rupture сканирует окно
    # истории (RUPTURE_WINDOW_S) обратно от конца; вызов на КАЖДЫЙ
    # кадр — O(окна) на кадр (при 1050 Гц и окне 5 с это ~5.5e6
    # сравнений/с и ~0.5 мс/кадр — заметная доля GUI-потока).
    # Разрыв — медленное событие (около-ноль держится RUPTURE_HOLD_S
    # = 1 с), поэтому переоценка 50 раз/с не меняет детекцию, но
    # снимает квадратичный по времени работы рост нагрузки.
    RUPTURE_CHECK_INTERVAL_S = 0.02

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
        # Остановить поддержание и запись. Измерение (поток кадров)
        # НЕ останавливаем: оператор должен видеть, как сила
        # восстанавливается; STOP можно нажать вручную.
        if self.maintain_active:
            self.maintain_active = False
            self.maintain_caught = False
            self.maintain_window = []
            self.reset_maintain_buffer()
            self.maintain_timer.stop()
            self.maintain_button.setText(
                "НАЧАТЬ ПОДДЕРЖИВАТЬ"
            )
            self.set_traverse_block_enabled(True)
            logger.info("MAINTAIN: остановлено (аварийный стопор)")

        if self.session_recorder.active:
            self.session_recorder.stop_session()
            self.last_session_label.setText(
                f"Последняя сессия: "
                f"{self.session_recorder.session_dir}"
            )
            self._set_record_button_state()

    def get_force_stop_rise_mm(self):
        # Отвод траверсы при стопоре по силе: мм вверх от текущей
        # позиции (быстро, скорость 5 мм/с).
        try:
            rise = float(
                self.force_stop_rise_edit.text().replace(",", ".")
            )
        except ValueError:
            rise = 5.0

        return max(0.1, rise)

    def get_traverse_min_mm(self):
        # Минимальный ход траверсы, мм (нижняя граница; вкладка
        # «Настройки»). Значение <= 0 фактически отключает защиту
        # (нарушение возможно лишь строго при позиции <= 0).
        try:
            return float(
                self.traverse_min_edit.text().replace(",", ".")
            )
        except ValueError:
            return 5.0

    def get_force_limit_n(self):
        # Верхний предел силы (защита датчика), Н — внутренние
        # единицы. None = защита отключена (значение <= 0).
        try:
            value = float(
                self.force_limit_edit.text().replace(",", ".")
            )
        except ValueError:
            return 45.0

        return value if value > 0 else None

    def check_force_stop(self, force_n, current_mm):
        # Стопор по силе: сила ≤ порога означает упор в нижний
        # концевик — датчик и образец под угрозой.
        #
        # Окно и полный набор действий — при ДОСТИЖЕНИИ условия
        # (переход «норма → нарушение»). Пока нарушение ДЕРЖИТСЯ
        # (сила не вышла из-под порога), отвод ПОВТОРЯЕТСЯ шагами
        # (шаг не чаще раза в 1 с, от ТЕКУЩЕЙ позиции) — иначе
        # одного шага может не хватить, чтобы выйти из зоны упора,
        # и траверса останется прижатой. Подсчёт циклов на время
        # аварии подавлен (см. graph_widget.add_frame).
        stop_n = self.get_force_stop_n()

        violating = (
            stop_n is not None
            and force_n <= stop_n
        )

        if not violating:
            if getattr(self, "_force_stop_active", False):
                logger.info(
                    "FORCE STOP: сила восстановилась, защита "
                    "перезаряжена"
                )
            self._force_stop_active = False
            self.force_graph._force_stop_active = False

            # Режим DataPipeline: гейт и в потоке-обработчике.
            if getattr(self, "data_pipeline", None) is not None:
                self.data_pipeline.set_force_stop_active(False)

            return

        now = time.time()

        # Повторные шаги отвода, пока нарушение держится:
        # не чаще раза в 1 с (кадры 20–25 Гц).
        if getattr(self, "_force_stop_active", False):
            if now - getattr(self, "_force_stop_last_t", 0.0) < 1.0:
                return

            self._force_stop_last_t = now

            logger.critical(
                f"FORCE STOP: нарушение держится "
                f"({force_n:.3f} N @ {current_mm:.3f} мм) — "
                f"повторный шаг отвода"
            )

            self._retreat_step(current_mm)
            return

        self._force_stop_active = True
        self._force_stop_last_t = now

        # Подавить подсчёт циклов на время аварии (детектор на
        # прижиме считает мусорные «циклы»).
        self.force_graph._force_stop_active = True

        # Режим DataPipeline: гейт и в потоке-обработчике.
        if getattr(self, "data_pipeline", None) is not None:
            self.data_pipeline.set_force_stop_active(True)

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
            self.reset_maintain_buffer()
            self.maintain_timer.stop()
            self.maintain_button.setText(
                "НАЧАТЬ ПОДДЕРЖИВАТЬ"
            )
            self.set_traverse_block_enabled(True)
            logger.info("MAINTAIN: остановлено (стопор по силе)")

        # 2) Быстрый отвод ВВЕРХ от текущей позиции на «Отвод при
        #    стопоре» мм (направление увеличения силы —
        #    MAINTAIN_UP_INCREASES_FORCE), не выше верхней границы
        #    хода. Скорость — 5 мм/с.
        self._retreat_step(current_mm)

        self.append_log(
            f"СТОПОР ПО СИЛЕ: {force_n:.3f} N ≤ {stop_n:.3f} N — "
            f"упор в нижний концевик. Траверса отводится вверх "
            f"шагами по {self.get_force_stop_rise_mm():.1f} мм, "
            f"пока сила не восстановится; поддержание и запись "
            f"остановлены."
        )

        # 3) Остановить запись сессии (измерение оставляем работать:
        #    поток кадров нужен оператору для контроля восстановления
        #    силы; STOP — вручную при необходимости).
        self._emergency_stop_all()

        logger.critical(
            "FORCE STOP: поддержание и запись остановлены, "
            "измерение продолжается"
        )

        # 4) Аварийное окно (последним — значения уже в логе).
        self.show_emergency_dialog(
            f"Сила {force_n:.3f} Н ниже порога {stop_n:.3f} Н — "
            f"упор в нижний концевик (стопор по силе). "
            f"Траверса отводится вверх шагами по "
            f"{self.get_force_stop_rise_mm():.1f} мм, пока сила "
            f"не восстановится.",
            force_n,
            current_mm,
        )

    def check_force_limit(self, force_n, current_mm):
        # ВЕРХНИЙ предел силы (защита датчика): сила ≥ лимита
        # (внутренние единицы — Н). Edge-triggered: полный набор
        # действий при переходе «норма → нарушение»; пока
        # нарушение держится, отвод в сторону УМЕНЬШЕНИЯ силы
        # повторяется шагами (не чаще раза в 1 с). Перезарядка —
        # при снижении ниже лимита С ГИСТЕРЕЗИСОМ (лимит−0.5 Н),
        # чтобы не дребезжать на границе.
        #
        # НЕ путать с rupture-детектором: тот ищет ПАДЕНИЕ силы
        # до около-нуля после нагрузки; здесь — превышение сверху
        # (в т.ч. при растяжении — это тоже штатная авария).
        limit_n = self.get_force_limit_n()

        if limit_n is None:
            self._force_limit_active = False
            return

        hysteresis_n = 0.5

        violating = force_n >= limit_n

        if not violating:
            if getattr(self, "_force_limit_active", False):
                # Гистерезис: держим флаг, пока сила не опустилась
                # ЗАМЕТНО ниже лимита (лимит − гистерезис).
                if force_n > limit_n - hysteresis_n:
                    return

                logger.info(
                    "FORCE LIMIT: сила снизилась ниже лимита — "
                    "защита перезаряжена"
                )

            self._force_limit_active = False
            return

        now = time.time()

        # Повторные шаги отвода, пока нарушение держится.
        if getattr(self, "_force_limit_active", False):
            if now - getattr(self, "_force_limit_last_t", 0.0) < 1.0:
                return

            self._force_limit_last_t = now

            logger.critical(
                f"FORCE LIMIT: нарушение держится "
                f"({force_n:.3f} N @ {current_mm:.3f} мм) — "
                f"повторный шаг отвода"
            )

            self._retreat_force_reduce_step(current_mm)
            return

        self._force_limit_active = True
        self._force_limit_last_t = now

        logger.critical(
            f"FORCE LIMIT: сила {force_n:.3f} N ≥ лимита "
            f"{limit_n:.3f} N (защита датчика), позиция "
            f"{current_mm:.3f} мм"
        )

        # 1) Остановить регулятор поддержания — его MOVE мог бы
        #    перебить аварийное перемещение или снова поднять силу.
        if self.maintain_active:
            self.maintain_active = False
            self.maintain_caught = False
            self.maintain_window = []
            self.reset_maintain_buffer()
            self.maintain_timer.stop()
            self.maintain_button.setText(
                "НАЧАТЬ ПОДДЕРЖИВАТЬ"
            )
            self.set_traverse_block_enabled(True)
            logger.info("MAINTAIN: остановлено (предел силы)")

        # 2) Отвод в сторону УМЕНЬШЕНИЯ силы (обратный знак к
        #    _retreat_step) шагами по «Отвод при стопоре» мм.
        self._retreat_force_reduce_step(current_mm)

        self.append_log(
            f"ПРЕДЕЛ СИЛЫ: {force_n:.3f} N ≥ {limit_n:.3f} N — "
            f"защита датчика. Траверса отводится в сторону "
            f"уменьшения силы шагами по "
            f"{self.get_force_stop_rise_mm():.1f} мм, пока сила "
            f"не снизится ниже лимита; поддержание и запись "
            f"остановлены."
        )

        # 3) Остановить запись сессии (измерение оставляем — как
        #    в остальных аварийных защитах).
        self._emergency_stop_all()

        logger.critical(
            "FORCE LIMIT: поддержание и запись остановлены, "
            "измерение продолжается"
        )

        # 4) Аварийное окно (последним — значения уже в логе).
        self.show_emergency_dialog(
            f"Сила {force_n:.3f} Н достигла верхнего предела "
            f"{limit_n:.3f} Н — защита датчика (предел силы). "
            f"Траверса отводится в сторону уменьшения силы "
            f"шагами по {self.get_force_stop_rise_mm():.1f} мм, "
            f"пока сила не снизится ниже лимита.",
            force_n,
            current_mm,
        )

    def check_rupture(self, force_n, now):
        # Детекция разрыва образца: резкое падение силы до
        # около-нуля после нагруженной сессии. Срабатывает ОДИН
        # раз до перезарядки (флаг _rupture_handled) и НЕ при
        # активной аварии стопора по силе.
        #
        # Логика: по буферу кадров графика (values/frame_times)
        # 1) пик силы за окно RUPTURE_WINDOW_S >= RUPTURE_MIN_PEAK_N
        #    (сессия нагружена);
        # 2) среднее |силы| за последние RUPTURE_HOLD_S <
        #    RUPTURE_NEAR_ZERO_N (около-ноль держится).
        if getattr(self, "_force_stop_active", False):
            return

        # BUG-18 (стенд 29-10-2026): при ПОДХОДЕ к цели (поддержание
        # активно, но цель ещё не поймана) сила закономерно около
        # нуля — детектор разрыва давал ложное срабатывание и
        # останавливал всё (траверса не едет). Разрыв ищем только
        # когда цель поймана (образец реально нагружен) или когда
        # поддержание вообще не активно.
        if (
                getattr(self, "maintain_active", False)
                and not getattr(self, "maintain_caught", False)
        ):
            return

        if getattr(self, "_rupture_handled", False):
            return

        # Троттлинг: тяжёлые обратные сканы окна — не чаще
        # RUPTURE_CHECK_INTERVAL_S. Критерии/пороги не меняются,
        # меняется лишь частота переоценки (см. константу).
        last_check = getattr(self, "_rupture_check_t", 0.0)

        if now - last_check < self.RUPTURE_CHECK_INTERVAL_S:
            return

        self._rupture_check_t = now

        graph = self.force_graph
        values = getattr(graph, "values", None)
        times = getattr(graph, "frame_times", None)

        if not values or not times:
            return

        total = min(len(values), len(times))

        if total <= 0:
            return

        near_zero_n = getattr(
            self, "rupture_near_zero_n", self.RUPTURE_NEAR_ZERO_N
        )
        min_peak_n = getattr(
            self, "rupture_min_peak_n", self.RUPTURE_MIN_PEAK_N
        )

        window_s = self.RUPTURE_WINDOW_S
        hold_s = self.RUPTURE_HOLD_S

        window_start = now - window_s
        hold_start = now - hold_s

        # 1) Пик силы за окно оценки.
        # Скан идёт с КОНЦА буфера (times возрастают) и
        # обрывается на первом кадре старше окна. Прежний скан
        # всего буфера был O(n) на КАЖДЫЙ кадр (O(n^2) за
        # сессию): при окне 120 с это ~72k итераций × 600 кадр/с
        # и заметная доля загрузки ядра на пустой работе.
        peak = None

        for index in range(total - 1, -1, -1):
            if times[index] < window_start:
                break

            value = values[index]

            if peak is None or value > peak:
                peak = value

        if peak is None or peak < min_peak_n:
            return

        # Окно удержания должно быть полностью покрыто данными
        # (иначе около-ноль «наберётся» раньше времени).
        if times[0] > hold_start:
            return

        # 2) Среднее |силы| за окно удержания (тот же обратный
        # скан с обрывом — порядок не важен, берётся среднее).
        hold_values = []

        for index in range(total - 1, -1, -1):
            if times[index] < hold_start:
                break

            hold_values.append(values[index])

        if not hold_values:
            return

        hold_mean_abs = (
            sum(abs(value) for value in hold_values)
            / len(hold_values)
        )

        if hold_mean_abs >= near_zero_n:
            return

        # Разрыв подтверждён — один раз до перезарядки.
        self._rupture_handled = True

        logger.critical(
            f"RUPTURE: сила упала до ~{hold_mean_abs:.4f} Н "
            f"(пик за {window_s:.0f} с = {peak:.4f} Н, порог "
            f"около-нуля {near_zero_n:.4f} Н) — разрыв образца"
        )

        self.append_log(
            "ОБРЫВ ОБРАЗЦА: сила упала до ~0 — финальное "
            "сохранение и остановка"
        )

        # 1) Остановить поддержание силы, если активно.
        if self.maintain_active:
            self.stop_maintain_force(
                reason="остановлено — разрыв образца"
            )

        # 2) Финальное сохранение данных с суффиксом _final.
        try:
            self.session_recorder.finalize_with_suffix("_final")
        except Exception as error:
            logger.error(
                f"RUPTURE: ошибка финального сохранения: {error}"
            )

        # 3) STOP измерения — то же, что кнопка STOP.
        self.send_stop()

    def _retreat_step(self, current_mm):
        # Один шаг отвода от текущей позиции вверх (в сторону
        # увеличения силы), не выше верхней границы хода.
        direction = (
            1 if self.MAINTAIN_UP_INCREASES_FORCE else -1
        )

        target_mm = (
            current_mm
            + direction * self.get_force_stop_rise_mm()
        )

        target_mm = min(
            target_mm,
            self.get_traverse_max_mm(),
        )

        self.send_command(
            f"MOVE_0_{target_mm:.6f}_"
            f"{self.MAINTAIN_AUTO_SPEED_MAX:.6f}_YYY"
        )

        logger.critical(
            f"FORCE STOP: быстрый отвод вверх MOVE до "
            f"{target_mm:.3f} мм"
        )

    def _retreat_force_reduce_step(self, current_mm):
        # Один шаг отвода от текущей позиции В СТОРОНУ УМЕНЬШЕНИЯ
        # силы (при MAINTAIN_UP_INCREASES_FORCE=True это вниз, в
        # обратную сторону к _retreat_step) — защита датчика от
        # верхнего предела силы. Не ниже минимального хода. STOP
        # после MOVE не отправляем (прошивка отменяет движение).
        direction = (
            -1 if self.MAINTAIN_UP_INCREASES_FORCE else 1
        )

        target_mm = (
            current_mm
            + direction * self.get_force_stop_rise_mm()
        )

        target_mm = max(
            target_mm,
            self.get_traverse_min_mm(),
        )

        self.send_command(
            f"MOVE_0_{target_mm:.6f}_"
            f"{self.MAINTAIN_AUTO_SPEED_MAX:.6f}_YYY"
        )

        logger.critical(
            f"FORCE LIMIT: отвод в сторону уменьшения силы "
            f"MOVE до {target_mm:.3f} мм"
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
            and current_mm >= limit_mm
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
            f"верхней границы {limit_mm:.1f} мм. Поддержание и запись "
            f"остановлены."
        )

        self._emergency_stop_all()

        self.show_emergency_dialog(
            f"Положение траверсы {current_mm:.3f} мм превысило "
            f"верхнюю границу {limit_mm:.1f} мм. Поддержание и "
            f"запись остановлены.",
            force_n,
            current_mm,
        )

    def check_traverse_min_limit(self, force_n, current_mm):
        # НИЖНЯЯ граница хода траверсы (минимальный ход, мм).
        # Зеркально check_traverse_limit: edge-triggered переход
        # «норма → нарушение» при позиции ≤ минимума. Ретрит НЕ
        # нужен: уход вверх (в сторону нормы) штатно разрешён.
        #
        # Подавление на время HOME / выхода на стартовую позицию
        # ниже границы (_traverse_min_home_override): иначе
        # калибровка (HOME уходит в ~0 мм) и старт ниже 5 мм
        # ложно срабатывали бы. Флаг снимается, когда позиция
        # поднимается ВЫШЕ границы (защита перезаряжается).
        min_mm = self.get_traverse_min_mm()

        if getattr(self, "_traverse_min_home_override", False):
            if current_mm is None or current_mm <= min_mm:
                return

            # Поднялись выше границы — штатное возвращение
            # завершено, проверка снова активна.
            self._traverse_min_home_override = False
            logger.info(
                "TRAVERSE MIN LIMIT: выход из зоны HOME — защита "
                "перезаряжена"
            )

        violating = (
            current_mm is not None
            and min_mm is not None
            and current_mm <= min_mm
        )

        if not violating:
            self._traverse_min_limit_active = False
            return

        if getattr(self, "_traverse_min_limit_active", False):
            return

        self._traverse_min_limit_active = True

        logger.critical(
            f"TRAVERSE MIN LIMIT: позиция {current_mm:.3f} мм "
            f"достигла нижней границы {min_mm:.1f} мм, сила "
            f"{force_n:.3f} N"
        )

        self.append_log(
            f"АВАРИЯ: позиция траверсы {current_mm:.3f} мм "
            f"достигла нижней границы хода {min_mm:.1f} мм. "
            f"Поддержание и запись остановлены, ручное "
            f"перемещение вниз заблокировано до восстановления."
        )

        self._emergency_stop_all()

        self.show_emergency_dialog(
            f"Положение траверсы {current_mm:.3f} мм достигло "
            f"нижней границы хода {min_mm:.1f} мм. Поддержание и "
            f"запись остановлены, движение вниз заблокировано.",
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

        # Стартовая позиция ниже минимального хода — подавить
        # защиту нижней границы на время выхода (как HOME).
        if target <= self.get_traverse_min_mm():
            self._traverse_min_home_override = True
            logger.info(
                "TRAVERSE MIN LIMIT: старт ниже нижней границы — "
                "проверка подавлена на время выхода"
            )

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

        # HOME уходит к ~0 мм (ниже минимального хода) — подавить
        # защиту нижней границы на время калибровки.
        self._traverse_min_home_override = True

        self.send_command(HOME_COMMAND)
        self.append_log("HOME: калибровка траверсы запущена")
        logger.info("TRAVERSE: HOME (калибровка) отправлена")

    def open_home_calibration_dialog(self):
        # Кнопка «Калибровка»: модальное окно с параметрами
        # команды и живым выводом силы/позиции вместо прямой
        # отправки HOME_COMMAND.
        dialog = HomeCalibrationDialog(self)
        dialog.exec_()

    def send_home_command(self, speed, detect_delta, retract_mm):
        # Калибровка траверсы по усилию из диалога: собрать
        # HOMEFORCE_<скорость>_<изменение силы>_<отвод>_YYY.
        if not self.connected or self.serial is None:
            self.append_log(
                "ОШИБКА: нет подключения к устройству"
            )
            return

        command = (
            f"HOMEFORCE_{speed:g}_{detect_delta:g}_"
            f"{retract_mm:g}_YYY"
        )

        # Калибровка по усилию уходит к нижнему концевику —
        # подавить защиту нижней границы на время HOMEFORCE.
        self._traverse_min_home_override = True

        self.send_command(command)
        self.append_log("HOME: калибровка траверсы запущена")
        logger.info(
            f"TRAVERSE: HOME (калибровка) отправлена: "
            f"скорость {speed:g} мм/с, порог силы {detect_delta:g}, "
            f"отвод {retract_mm:g} мм"
        )

