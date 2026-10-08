"""TraverseRegulatorMixin — вынесен из calibration_window.py механическим
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



class TraverseRegulatorMixin:
    # ========================================================
    # DISP TRAVERS
    # ========================================================
    def move_traverse_to_target(self):
        # Ручное перемещение заблокировано, пока активна авария
        # (стопор по силе / превышение границы): иначе
        # editingFinished поля цели (потеря фокуса при открытии
        # модального окна) повторно отправляет старую цель и
        # перебивает аварийный отвод.
        if (
                getattr(self, "_force_stop_active", False)
                or getattr(self, "_traverse_limit_active", False)
        ):
            self.append_log(
                "ОТМЕНА: активна авария (стопор по силе или "
                "верхняя граница) — ручное перемещение "
                "заблокировано до восстановления"
            )
            logger.warning(
                "TRAVERSE: ручной MOVE заблокирован активной аварией"
            )
            return

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


