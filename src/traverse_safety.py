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

        self.send_command(command)
        self.append_log("HOME: калибровка траверсы запущена")
        logger.info(
            f"TRAVERSE: HOME (калибровка) отправлена: "
            f"скорость {speed:g} мм/с, порог силы {detect_delta:g}, "
            f"отвод {retract_mm:g} мм"
        )

