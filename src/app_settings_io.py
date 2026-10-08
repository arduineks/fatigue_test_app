"""AppSettingsIOMixin — вынесен из calibration_window.py механическим
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



class AppSettingsIOMixin:
    # ========================================================
    # APP SETTINGS INI
    # ========================================================

    def browse_save_path(self):

        directory = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку для сохранения сессий",
            self.save_path_edit.text() or str(REPO_ROOT),
        )

        if directory:
            self.save_path_edit.setText(directory)
            self.save_app_settings()

    def on_record_interval_changed(self, text):

        hint = format_interval_hint(text)

        if parse_interval(text) is None:
            self.interval_hint_label.setStyleSheet(
                "color: #FF4D4D;"
            )
        else:
            self.interval_hint_label.setStyleSheet(
                "color: #8B9AA5;"
            )

        self.interval_hint_label.setText(hint)

        # Интервал читается live: пересоздать таймер flush.
        seconds = parse_interval(text)

        if seconds is not None:
            self.session_recorder.set_flush_interval(seconds)

    def load_app_settings(self):

        config = configparser.ConfigParser()

        if not APP_SETTINGS_PATH.exists():
            logger.info(
                f"APP SETTINGS INI not found: {APP_SETTINGS_PATH}"
            )
            return

        try:
            config.read(
                APP_SETTINGS_PATH,
                encoding="utf-8"
            )

            if not config.has_section("RECORDING"):
                return

            section = config["RECORDING"]

            self.specimen_name_edit.setText(
                section.get("specimen_name", fallback="Образец-1")
            )

            self.save_path_edit.setText(
                section.get(
                    "save_path",
                    fallback=str(REPO_ROOT / "Saved data"),
                )
            )

            # Сначала применяем значения из файла во ВСЕ поля,
            # включая траверсу: on_record_interval_changed ниже
            # вызывает save_app_settings, и он записал бы в файл
            # ещё не загруженные дефолты полей траверсы.
            if config.has_section("TRAVERSE"):

                traverse = config["TRAVERSE"]

                self.traverse_max_edit.setText(
                    traverse.get("max_mm", fallback="145")
                )

                self.traverse_start_edit.setText(
                    traverse.get("start_mm", fallback="10")
                )

                self.traverse_start_speed_edit.setText(
                    traverse.get("start_speed_mm_s", fallback="0.5")
                )

                self.force_stop_edit.setText(
                    traverse.get("force_stop_n", fallback="-5")
                )

                self.force_stop_rise_edit.setText(
                    traverse.get("force_stop_rise_mm", fallback="5")
                )

            if config.has_section("RUPTURE"):

                rupture = config["RUPTURE"]

                try:
                    self.rupture_near_zero_n = float(
                        rupture.get(
                            "near_zero_n",
                            fallback=str(self.RUPTURE_NEAR_ZERO_N),
                        )
                    )
                except ValueError:
                    pass

                try:
                    self.rupture_min_peak_n = float(
                        rupture.get(
                            "min_peak_n",
                            fallback=str(self.RUPTURE_MIN_PEAK_N),
                        )
                    )
                except ValueError:
                    pass

            if config.has_section("SPECIMEN"):

                specimen = config["SPECIMEN"]

                self.specimen_width_edit.setText(
                    specimen.get(
                        "width_mm",
                        fallback=self.specimen_width_edit.text(),
                    )
                )

                self.specimen_thickness_edit.setText(
                    specimen.get(
                        "thickness_mm",
                        fallback=self.specimen_thickness_edit.text(),
                    )
                )

                # МПа-режим зависит от площади — пересчитать
                # после установки размеров.
                self.update_specimen_area()

            if config.has_section("CYCLE_TARGET"):

                cycle_target = config["CYCLE_TARGET"]

                try:
                    sigma_max_n = float(
                        cycle_target.get(
                            "sigma_max_n",
                            fallback="0",
                        )
                    )
                except ValueError:
                    sigma_max_n = 0.0

                self.cycle_target_sigma_max_n = sigma_max_n

                # σ_max — в текущих единицах отображения.
                self.cycle_target_sigma_max_edit.setText(
                    f"{self.convert_force_value(sigma_max_n):.3f}"
                )

                self.cycle_target_r_edit.setText(
                    cycle_target.get("r", fallback="0")
                )

                # Без сигналов: иначе toggled вызвал бы
                # save_app_settings до загрузки остальных полей.
                self.cycle_target_lines_check.blockSignals(True)

                self.cycle_target_lines_check.setChecked(
                    cycle_target.get("lines_on", fallback="1") == "1"
                )

                self.cycle_target_lines_check.blockSignals(False)

            # σ_min и целевые линии — после загрузки полей.
            self._update_cycle_targets()

            if config.has_section("HOME"):

                home = config["HOME"]

                # Параметры диалога «Калибровка» (HOMEFORCE):
                # скорость мм/с, порог изменения силы, отвод мм.
                self.home_speed_text = (
                    home.get("speed_mm_s") or "0.1"
                )

                self.home_detect_text = (
                    home.get("detect_delta") or "0.1"
                )

                self.home_retract_text = (
                    home.get("retract_mm") or "5"
                )

            self.record_interval_edit.setText(
                section.get("record_interval", fallback="30м")
            )

            # textChanged мог не сработать при установке
            # идентичного текста — обновим зависимые элементы.
            self.on_record_interval_changed(
                self.record_interval_edit.text()
            )

        except Exception as e:

            logger.error(f"APP SETTINGS load ERROR: {e}")

    def save_app_settings(self):

        config = configparser.ConfigParser()

        if APP_SETTINGS_PATH.exists():
            config.read(
                APP_SETTINGS_PATH,
                encoding="utf-8"
            )

        if not config.has_section("RECORDING"):
            config.add_section("RECORDING")

        config["RECORDING"]["specimen_name"] = (
            self.specimen_name_edit.text()
        )

        config["RECORDING"]["record_interval"] = (
            self.record_interval_edit.text()
        )

        config["RECORDING"]["save_path"] = (
            self.save_path_edit.text()
        )

        if not config.has_section("TRAVERSE"):
            config.add_section("TRAVERSE")

        config["TRAVERSE"]["max_mm"] = (
            self.traverse_max_edit.text()
        )

        config["TRAVERSE"]["start_mm"] = (
            self.traverse_start_edit.text()
        )

        config["TRAVERSE"]["start_speed_mm_s"] = (
            self.traverse_start_speed_edit.text()
        )

        config["TRAVERSE"]["force_stop_n"] = (
            self.force_stop_edit.text()
        )

        config["TRAVERSE"]["force_stop_rise_mm"] = (
            self.force_stop_rise_edit.text()
        )

        if not config.has_section("SPECIMEN"):
            config.add_section("SPECIMEN")

        config["SPECIMEN"]["width_mm"] = (
            self.specimen_width_edit.text()
        )

        config["SPECIMEN"]["thickness_mm"] = (
            self.specimen_thickness_edit.text()
        )

        if not config.has_section("CYCLE_TARGET"):
            config.add_section("CYCLE_TARGET")

        # Каноническое σ_max хранится в Н (независимо от
        # текущих единиц отображения).
        if self.cycle_target_sigma_max_n is not None:
            config["CYCLE_TARGET"]["sigma_max_n"] = (
                f"{self.cycle_target_sigma_max_n:.6g}"
            )
        else:
            config["CYCLE_TARGET"]["sigma_max_n"] = ""

        config["CYCLE_TARGET"]["r"] = (
            self.cycle_target_r_edit.text()
        )

        config["CYCLE_TARGET"]["lines_on"] = (
            "1"
            if self.cycle_target_lines_check.isChecked()
            else "0"
        )

        if not config.has_section("HOME"):
            config.add_section("HOME")

        # Параметры диалога «Калибровка» (HOMEFORCE).
        config["HOME"]["speed_mm_s"] = str(self.home_speed_text)
        config["HOME"]["detect_delta"] = str(self.home_detect_text)
        config["HOME"]["retract_mm"] = str(self.home_retract_text)

        if not config.has_section("RUPTURE"):
            config.add_section("RUPTURE")

        # Пороги детекции разрыва образца (Н): около-нулевое
        # значение силы и минимальный пик нагруженной сессии.
        config["RUPTURE"]["near_zero_n"] = (
            f"{getattr(self, 'rupture_near_zero_n', self.RUPTURE_NEAR_ZERO_N):.6g}"
        )

        config["RUPTURE"]["min_peak_n"] = (
            f"{getattr(self, 'rupture_min_peak_n', self.RUPTURE_MIN_PEAK_N):.6g}"
        )

        try:
            with open(
                    APP_SETTINGS_PATH,
                    "w",
                    encoding="utf-8"
            ) as f:
                config.write(f)

        except Exception as e:

            logger.error(f"APP SETTINGS save ERROR: {e}")

