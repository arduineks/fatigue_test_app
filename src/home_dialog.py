"""Диалог калибровки траверсы по усилию (HOME FORCE).

Модальное окно кнопки «Калибровка» (вкладка «Настройки», группа
«ТРАВЕРСА»). Вместо прямой отправки HOME_COMMAND из send_home окно
собирает команду HOMEFORCE_<скорость мм/с>_<детектируемое изменение
силы>_<отвод мм после триггера>_YYY из полей ввода и показывает
живой вывод текущей силы (в выбранных единицах Н/МПа) и положения
траверсы. Параметры сохраняются в секции [HOME] app_settings.ini
(см. AppSettingsIOMixin).

Модуль импортирует только PyQt5 и logging_setup — обратной
зависимости от main_window/traverse_safety нет (защита от циклов).
"""

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QLineEdit,
    QFrame,
)

from src.logging_setup import logger


class HomeCalibrationDialog(QDialog):
    # Модальное окно параметров калибровки траверсы по усилию
    # с живым выводом силы/позиции.

    def __init__(self, parent):
        super().__init__(parent)

        # Родитель — MainWindow: источник live-данных
        # (last_force_n, last_current_mm) и конвертеров единиц,
        # а также приёмник команды HOMEFORCE.
        self.parent_window = parent

        self.setWindowTitle(
            "Калибровка траверсы по усилию"
        )

        self.setModal(True)

        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # --------------------------------------------------
        # Параметры команды
        # --------------------------------------------------

        params_group = QGroupBox("ПАРАМЕТРЫ КАЛИБРОВКИ")

        params_form = QGridLayout(params_group)
        params_form.setContentsMargins(10, 10, 10, 10)
        params_form.setHorizontalSpacing(8)
        params_form.setVerticalSpacing(4)

        speed_stack = QVBoxLayout()
        speed_stack.setSpacing(4)
        speed_stack.addWidget(
            self._caption("Скорость, мм/с")
        )

        self.speed_edit = QLineEdit()
        self.speed_edit.setText(
            str(getattr(parent, "home_speed_text", "0.1"))
        )
        self.speed_edit.setMaximumWidth(120)
        speed_stack.addWidget(self.speed_edit)

        params_form.addLayout(speed_stack, 0, 0)

        detect_stack = QVBoxLayout()
        detect_stack.setSpacing(4)
        detect_stack.addWidget(
            self._caption("Изменение силы (порог)")
        )

        self.detect_edit = QLineEdit()
        self.detect_edit.setText(
            str(getattr(parent, "home_detect_text", "0.1"))
        )
        self.detect_edit.setMaximumWidth(120)
        detect_stack.addWidget(self.detect_edit)

        params_form.addLayout(detect_stack, 0, 1)

        retract_stack = QVBoxLayout()
        retract_stack.setSpacing(4)
        retract_stack.addWidget(
            self._caption("Отвод после триггера, мм")
        )

        self.retract_edit = QLineEdit()
        self.retract_edit.setText(
            str(getattr(parent, "home_retract_text", "5"))
        )
        self.retract_edit.setMaximumWidth(120)
        retract_stack.addWidget(self.retract_edit)

        params_form.addLayout(retract_stack, 0, 2)

        layout.addWidget(params_group)

        # --------------------------------------------------
        # Живой вывод силы / позиции
        # --------------------------------------------------

        live_group = QGroupBox("ТЕКУЩИЕ ЗНАЧЕНИЯ")

        live_layout = QHBoxLayout(live_group)
        live_layout.setContentsMargins(10, 10, 10, 10)
        live_layout.setSpacing(8)

        self.force_value_label = self._create_value_card(
            "СИЛА", "0.000 N", "#19E6FF"
        )
        live_layout.addWidget(self.force_value_label.card_frame)

        self.position_value_label = self._create_value_card(
            "ПОЛОЖЕНИЕ ТРАВЕРСЫ", "0.000 mm", "#19E6FF"
        )
        live_layout.addWidget(self.position_value_label.card_frame)

        layout.addWidget(live_group)

        # Подсказка/ошибка валидации.
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: #8B9AA5;")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        # --------------------------------------------------
        # Кнопки
        # --------------------------------------------------

        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)

        self.start_button = QPushButton("Запустить калибровку")
        self.start_button.clicked.connect(self.start_calibration)
        buttons_layout.addWidget(self.start_button)

        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.close)
        buttons_layout.addWidget(self.close_button)

        layout.addLayout(buttons_layout)

        # --------------------------------------------------
        # Живое обновление (100 мс — как measurement_timer)
        # --------------------------------------------------

        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self.update_live_values)
        self.live_timer.start(100)

        self.update_live_values()

    # ------------------------------------------------------
    # Помощники UI (стиль приложения)
    # ------------------------------------------------------

    def _caption(self, text):
        # Серая подпись поля — как _field_caption в окне.
        caption = QLabel(text)
        caption.setStyleSheet("color: #8B9AA5;")
        return caption

    def _create_value_card(self, title, value, color):
        # Карточка значения в стиле create_cycle_card: фон
        # #08151A, рамка #1C7F90, значение цветом линии,
        # подпись серая.

        card = QFrame()
        card.setFixedHeight(64)
        card.setStyleSheet(
            "QFrame {"
            "background: #08151A;"
            "border: 1px solid #1C7F90;"
            "border-radius: 5px;"
            "}"
        )

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(6, 5, 6, 5)
        card_layout.setSpacing(2)

        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet(
            "color: #8B9AA5; "
            "font-size: 9pt; "
            "font-weight: bold; "
            "border: none; "
            "background: transparent;"
        )

        value_label = QLabel(value)
        value_label.setAlignment(Qt.AlignCenter)
        value_label.setMinimumHeight(32)
        value_label.setStyleSheet(
            f"color: {color}; "
            "font-size: 15pt; "
            "font-weight: bold; "
            "border: none; "
            "background: transparent;"
        )

        card_layout.addWidget(title_label)
        card_layout.addWidget(value_label)

        value_label.card_frame = card
        return value_label

    # ------------------------------------------------------
    # Живые данные
    # ------------------------------------------------------

    def update_live_values(self):
        # Сила и позиция — из атрибутов MainWindow, обновляемых
        # serial-путём (last_force_n, last_current_mm).

        parent = self.parent_window

        force_n = getattr(parent, "last_force_n", None)

        if force_n is not None:
            suffix = parent.current_force_suffix()
            self.force_value_label.setText(
                f"{parent.convert_force_value(force_n):.3f} {suffix}"
            )

        current_mm = getattr(parent, "last_current_mm", None)

        if current_mm is not None:
            self.position_value_label.setText(
                f"{current_mm:.3f} mm"
            )

    # ------------------------------------------------------
    # Запуск калибровки
    # ------------------------------------------------------

    def _parse_field(self, edit):
        # Число поля с поддержкой запятой; None при ошибке.
        try:
            return float(edit.text().strip().replace(",", "."))
        except (ValueError, AttributeError):
            return None

    def start_calibration(self):
        # Отправить HOMEFORCE из введённых параметров. Окно НЕ
        # закрывается: оператор следит за силой/позицией.

        speed = self._parse_field(self.speed_edit)
        detect = self._parse_field(self.detect_edit)
        retract = self._parse_field(self.retract_edit)

        if speed is None or detect is None or retract is None:
            self.hint_label.setStyleSheet("color: #FF4D4D;")
            self.hint_label.setText(
                "Ошибка: параметры должны быть числами."
            )
            return

        if speed <= 0 or detect <= 0 or retract < 0:
            self.hint_label.setStyleSheet("color: #FF4D4D;")
            self.hint_label.setText(
                "Ошибка: скорость и порог силы должны быть > 0, "
                "отвод — не отрицательный."
            )
            return

        # Запомнить параметры для app_settings.ini и сохранить.
        parent = self.parent_window
        parent.home_speed_text = self.speed_edit.text()
        parent.home_detect_text = self.detect_edit.text()
        parent.home_retract_text = self.retract_edit.text()

        if hasattr(parent, "save_app_settings"):
            parent.save_app_settings()

        parent.send_home_command(speed, detect, retract)

        self.hint_label.setStyleSheet("color: #39FF88;")
        self.hint_label.setText(
            f"Команда отправлена: HOMEFORCE_{speed:g}_"
            f"{detect:g}_{retract:g}_YYY"
        )

    # ------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------

    def closeEvent(self, event):
        # Остановить таймер живого обновления при закрытии.
        self.live_timer.stop()
        super().closeEvent(event)
