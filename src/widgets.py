"""ToggleSwitch и CollapsibleGroupBox — вынесены из
calibration_window.py механическим сплитом (байт-в-байт).
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

class ToggleSwitch(QCheckBox):
    # Тумблер с бегунком: подложка-капсула, светлый
    # круглый бегунок, при включении сдвигается вправо,
    # подложка подсвечивается.
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        text = self.text()
        track_w = 44
        track_h = 24
        track_y = (
                (self.height() - track_h) // 2
        )

        checked = self.isChecked()

        if checked:
            track_color = QColor("#1C7F90")
            track_border = QColor("#19E6FF")
        else:
            track_color = QColor("#08151A")
            track_border = QColor("#71808C")

        if self.isEnabled() and self.underMouse():
            track_border = QColor("#FFFFFF")

        painter.setPen(
            QPen(track_border, 1)
        )
        painter.setBrush(track_color)
        painter.drawRoundedRect(
            0,
            track_y,
            track_w,
            track_h,
            track_h // 2,
            track_h // 2,
        )

        # Бегунок: 3 px от края дорожки.
        margin = 3
        knob_d = track_h - 2 * margin

        if checked:
            knob_x = track_w - margin - knob_d
        else:
            knob_x = margin

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#F2F5F7"))
        painter.drawEllipse(
            knob_x,
            track_y + margin,
            knob_d,
            knob_d,
        )

        if text:
            painter.setPen(
                QPen(QColor("#F2F5F7"))
            )

            painter.drawText(
                QRectF(
                    track_w + 10,
                    0,
                    self.width() - track_w - 10,
                    self.height(),
                ),
                Qt.AlignVCenter | Qt.AlignLeft,
                text,
            )

    def sizeHint(self):
        fm = self.fontMetrics()
        width = (
                44
                + 10
                + (
                    fm.horizontalAdvance(self.text())
                    if self.text()
                    else 0
                )
                + 8
        )

        return QSize(width, max(26, fm.height() + 6))


class CollapsibleGroupBox(QGroupBox):
    # Группа-аккордеон: маркер в заголовке ("V" — развёрнута,
    # ">" — свёрнута), клик по заголовку переключает состояние.
    # Содержимое скрывается, кроме виджетов, зарегистрированных
    # через keep_visible() (окна со значением).

    TITLE_CLICK_HEIGHT = 22

    def __init__(self, title="", parent=None):
        super().__init__(title, parent)
        # Высота по содержимому: лишнее место уходит в stretch,
        # при сворачивании группа сжимается без пустот.
        self.setSizePolicy(
            QSizePolicy.Preferred,
            QSizePolicy.Maximum,
        )
        # objectName для QSS-сдвига заголовка вправо
        # (маркер рисуется поверх группы).
        self.setObjectName("collapsible")
        self._base_title = title
        self._keep = []
        self._expanded = True

    def paintEvent(self, event):
        # Маркер состояния: рамка с "V" (развёрнуто) или
        # ">" (свёрнуто) слева от заголовка, обведён рамкой,
        # чтобы выделяться из текста заголовка.
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        marker_rect = QRectF(10, 3, 18, 16)

        painter.setPen(
            QPen(QColor("#1C7F90"), 1)
        )
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            marker_rect,
            3,
            3,
        )

        painter.setPen(
            QPen(QColor("#19E6FF"))
        )

        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)

        painter.drawText(
            marker_rect,
            Qt.AlignCenter,
            "V" if self._expanded else ">",
        )

    def keep_visible(self, widget):
        self._keep.append(widget)

    def _full_title(self):
        # Маркер рисуется в paintEvent, текст заголовка —
        # без префикса.
        return self._base_title

    def mousePressEvent(self, event):
        if (
                event.button() == Qt.LeftButton
                and event.pos().y() <= self.TITLE_CLICK_HEIGHT
        ):
            self.toggle_collapsed()
        super().mousePressEvent(event)

    def toggle_collapsed(self):
        self._expanded = not self._expanded
        self.setTitle(
            self._full_title()
        )
        self._apply_state()

    def _apply_state(self):
        def walk(layout):
            for i in range(layout.count()):
                item = layout.itemAt(i)
                widget = item.widget()
                if widget is not None:
                    widget.setVisible(
                        self._expanded
                        or widget in self._keep
                    )
                else:
                    sub = item.layout()
                    if sub is not None:
                        walk(sub)
        lay = self.layout()
        if lay is not None:
            walk(lay)


