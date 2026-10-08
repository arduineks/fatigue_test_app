"""Запись сессии испытания и экспорт отчёта.

Модуль этапа 0: поцикловая запись данных в CSV с буферизацией и
flush по таймеру, старт/стоп сессии с привязкой к образцу, экспорт
отчёта в PDF (без новых зависимостей — QPdfWriter/QPainter) и CSV.

Правила проекта:
- данные внутри хранятся только в Н (MAX/MIN/MID/AMP);
- слой отображения (Н/МПа) применяется при формировании строк отчёта
  в вызывающем коде (MainWindow), а не здесь;
- serial-пути здесь отсутствуют по определению — модуль чисто UI/файловый.
"""

import csv
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QTimer, Qt, QRectF
from PyQt5.QtGui import (
    QPdfWriter,
    QPageSize,
    QPageLayout,
    QPainter,
    QFont,
    QColor,
    QPen,
    QPixmap,
)

from src.logging_setup import logger


# ============================================================
# ПАРСЕР ИНТЕРВАЛА ЗАПИСИ
# ============================================================

# Универсальный формат «<число><суффикс>»:
#   с / сек / s      — секунды
#   м / мин / m      — минуты
#   ч / h            — часы
#   д / d            — сутки
# Регистр не важен, пробелы допустимы. Голое число = МИНУТЫ.
_INTERVAL_RE = re.compile(r"^(\d+(?:[.,]\d+)?)([а-яa-z]*)$")

_SECONDS_SUFFIXES = ("с", "сек", "s")
_MINUTES_SUFFIXES = ("м", "мин", "m", "")
_HOURS_SUFFIXES = ("ч", "h")
_DAYS_SUFFIXES = ("д", "d")


def parse_interval(text):
    """Разобрать интервал записи в секунды.

    Возвращает число секунд (int >= 1) или None, если строку
    распознать нельзя. Чистая функция без побочных эффектов.
    """
    if text is None:
        return None

    raw = str(text).strip().lower().replace(" ", "")
    if not raw:
        return None

    match = _INTERVAL_RE.match(raw)
    if match is None:
        return None

    number_text = match.group(1).replace(",", ".")
    suffix = match.group(2)

    try:
        value = float(number_text)
    except ValueError:
        return None

    if value <= 0:
        return None

    if suffix in _SECONDS_SUFFIXES:
        seconds = value
    elif suffix in _MINUTES_SUFFIXES:
        seconds = value * 60.0
    elif suffix in _HOURS_SUFFIXES:
        seconds = value * 3600.0
    elif suffix in _DAYS_SUFFIXES:
        seconds = value * 86400.0
    else:
        return None

    seconds = int(round(seconds))
    if seconds < 1:
        return None
    return seconds


def format_interval_hint(text):
    """Человекочитаемая расшифровка интервала для подсказки.

    Возвращает «6 ч = 21600 с» для корректного и
    «некорректное значение» — для нераспознанного.
    """
    seconds = parse_interval(text)
    if seconds is None:
        return "некорректное значение"

    unit = str(text).strip().lower().replace(" ", "")
    match = _INTERVAL_RE.match(unit)
    if match is None:
        return "некорректное значение"

    value_text = match.group(1).replace(",", ".")
    suffix = match.group(2)

    if suffix in _SECONDS_SUFFIXES:
        unit_label = "с"
    elif suffix in _MINUTES_SUFFIXES:
        unit_label = "мин"
    elif suffix in _HOURS_SUFFIXES:
        unit_label = "ч"
    else:
        unit_label = "д"

    return f"{value_text} {unit_label} = {seconds} с"


def _safe_component(name):
    """Убрать из имени компонента символы, недопустимые в путях."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(name)).strip()
    return cleaned or "Образец"


# ============================================================
# ЗАПИСЬ СЕССИИ
# ============================================================

CSV_HEADER = [
    "Время,с",
    "N цикла",
    "MAX,Н",
    "MIN,Н",
    "MID,Н",
    "AMP,Н",
    "Позиция,мм",
]


class SessionRecorder:
    """Поцикловая запись сессии испытания с буферизацией и экспорт.

    Буфер наполняется через add_cycle_row и дописывается в data.csv
    по таймеру (flush). Отдельный deque history хранит строки за всю
    сессию для отчёта «последние 30 секунд». Все числовые значения
    силы — в Н.
    """

    def __init__(self, parent=None):
        self.base_path = None
        self.specimen_name = None
        self.session_dir = None
        self.data_path = None
        self.active = False
        # Момент начала записи (None до start_session).
        self.session_start_time = None

        # Буфер на дозапись в файл и история за сессию.
        self.buffer = []
        self.history = deque(maxlen=200000)

        self.rows_written = 0

        # Поставщик кадров данных графика: callable(seconds) →
        # список кортежей (t, raw, filtered, force_n, mm). Задаётся
        # MainWindow; используется для периодической записи кадров
        # в frames.csv с интервалом flush.
        self.frame_provider = None

        # Watermark времени последнего записанного кадра (unix).
        self._last_frame_t = 0.0

        # Срез кадров, взятый текущим flush (frames.csv папки).
        self._pending_frames = None

        # Интервал flush по умолчанию — 30 минут.
        self.flush_interval_s = 1800

        self.flush_timer = QTimer(parent)
        self.flush_timer.timeout.connect(self.flush)

    # --------------------------------------------------------
    # Инфраструктура
    # --------------------------------------------------------

    def set_flush_interval(self, seconds):
        """Задать интервал flush (в секундах); таймер пересоздаётся.

        Интервал читается live: если сессия активна — таймер
        перезапускается с новым периодом.
        """
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return

        if seconds < 1:
            return

        was_active = self.flush_timer.isActive()
        self.flush_timer.stop()
        self.flush_interval_s = seconds
        self.flush_timer.setInterval(seconds * 1000)

        if was_active and self.active:
            self.flush_timer.start()

        logger.info(f"RECORDER: интервал записи = {seconds} с")

    def set_frame_provider(self, provider):
        """Задать поставщика кадров данных графика (callable)."""
        self.frame_provider = provider

    # --------------------------------------------------------
    # Старт / стоп сессии
    # --------------------------------------------------------

    def start_session(self, base_path, specimen_name):
        """Начать запись: папки создаются НА КАЖДЫЙ интервал.

        Каждые record_interval секунд создаётся новая папка
        <base_path>/<ИмяОбразца>_дд-мм-гггг_ЧЧ-ММ-СС[_N], в неё
        пишется срез за интервал: frames.csv (кадры) и data.csv
        (поцикловая сводка). Первая папка создаётся сразу.
        Возвращает путь к первой папке.
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.specimen_name = specimen_name

        # Момент начала записи: база для elapsed_s, когда запись
        # идёт без запуска измерения кнопкой START.
        self.session_start_time = time.time()

        # Новый сеанс — watermark кадров заново (кадры, пришедшие
        # до старта записи, в папки не попадают).
        self._last_frame_t = time.time()

        self.buffer = []
        self.history.clear()
        self.rows_written = 0
        self.active = True

        self.flush_timer.setInterval(
            max(1, self.flush_interval_s * 1000)
        )
        self.flush_timer.start()

        # Первая папка — сразу (срез стартового интервала
        # будет записан следующим flush'ем).
        self.session_dir = self._next_folder()
        self.data_path = self.session_dir / "data.csv"
        with open(
                self.data_path, "w", newline="", encoding="utf-8"
        ) as handle:
            csv.writer(handle).writerow(CSV_HEADER)

        logger.info(f"RECORDER: запись начата, папка {self.session_dir}")
        return str(self.session_dir)

    def _next_folder(self):
        # Новая папка по правилам имени; суффикс _2, _3, ... при
        # совпадении (две папки в одну секунду).
        stamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
        folder = f"{_safe_component(self.specimen_name)}_{stamp}"

        candidate = self.base_path / folder
        suffix = 2
        while candidate.exists():
            candidate = self.base_path / f"{folder}_{suffix}"
            suffix += 1

        candidate.mkdir(parents=True)
        return candidate

    def stop_session(self):
        """Завершить запись: последний срез и остановка таймера."""
        self.flush()
        self.active = False
        self.flush_timer.stop()
        logger.info(
            f"RECORDER: запись остановлена, "
            f"записано цикл-строк {self.rows_written}"
        )

    # --------------------------------------------------------
    # Поцикловые данные
    # --------------------------------------------------------

    def add_cycle_row(self, row):
        """Добавить поцикловую строку в буфер и историю.

        row — dict с ключами elapsed_s, n, max_n, min_n, mid_n, amp_n
        (pos_mm — позиция траверсы в момент завершения цикла, мм).
        """
        if not self.active:
            return

        self.buffer.append(row)
        self.history.append(row)

    def flush(self):
        """Срез за прошедший интервал — в НОВУЮ папку.

        Каждые record_interval секунд создаётся новая папка, в неё:
        - frames.csv — кадры данных графика за интервал
          (пишутся всегда, независимо от завершения циклов);
        - data.csv — поцикловая сводка за интервал (строки,
          накопившиеся с прошлой записи).

        Если данных за интервал нет (ни кадров, ни циклов) —
        папка не создаётся. Возвращает число цикл-строк.
        """
        wrote = 0

        if self.active and self.buffer:
            pending = self.buffer
            self.buffer = []
            wrote = len(pending)
            self.rows_written += len(pending)
        else:
            pending = []

        has_frames = self._take_frame_slice()

        if not (pending or has_frames):
            # Данных за интервал нет — папку не создаём.
            return 0

        # Новая папка на этот срез.
        self.session_dir = self._next_folder()
        self.data_path = self.session_dir / "data.csv"

        try:
            with open(
                    self.data_path, "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.writer(handle)
                writer.writerow(CSV_HEADER)
                for row in pending:
                    pos = row.get("pos_mm")
                    writer.writerow([
                        f"{row.get('elapsed_s', 0.0):.3f}",
                        row.get("n", ""),
                        f"{row.get('max_n', 0.0):.4f}",
                        f"{row.get('min_n', 0.0):.4f}",
                        f"{row.get('mid_n', 0.0):.4f}",
                        f"{row.get('amp_n', 0.0):.4f}",
                        f"{pos:.3f}" if pos is not None else "",
                    ])

            if self._pending_frames is not None:
                frames_path = self.session_dir / "frames.csv"
                with open(
                        frames_path, "w", newline="", encoding="utf-8"
                ) as handle:
                    writer = csv.writer(handle)
                    writer.writerow(
                        ["Время(unix)", "RAW", "Фильтр", "Сила,Н",
                         "Позиция,мм"]
                    )
                    for t, raw, filtered, force_n, pos in (
                            self._pending_frames
                    ):
                        writer.writerow([
                            f"{t:.3f}",
                            f"{raw:.0f}",
                            f"{filtered:.0f}",
                            f"{force_n:.6f}",
                            (
                                f"{pos:.3f}"
                                if pos is not None
                                else ""
                            ),
                        ])

            logger.info(
                f"RECORDER: срез записан, папка {self.session_dir} "
                f"(цикл-строк {len(pending)}, кадров "
                f"{len(self._pending_frames or [])})"
            )
        except OSError as error:
            # Данные не теряем — возвращаем обратно в буфер.
            self.buffer = pending + self.buffer
            self.rows_written -= len(pending)
            logger.error(f"RECORDER: ошибка записи среза: {error}")

        self._pending_frames = None
        return wrote

    def _take_frame_slice(self):
        # Взять кадры, появившиеся с прошлой записи (watermark по
        # unix-времени кадра) → self._pending_frames. True, если
        # срез не пуст.
        self._pending_frames = None

        if (
                not self.active
                or self.frame_provider is None
        ):
            return False

        try:
            frames = self.frame_provider(self.flush_interval_s)
        except Exception as error:
            logger.error(f"RECORDER: ошибка получения кадров: {error}")
            return False

        new_frames = [
            f for f in frames
            if f[0] > self._last_frame_t
        ]

        if not new_frames:
            return False

        self._pending_frames = new_frames
        # Watermark — по последнему взятому кадру.
        self._last_frame_t = new_frames[-1][0]
        return True

    def recent_rows(self, seconds=30.0):
        """Строки за последние N секунд (по времени последней записи)."""
        if not self.history:
            return []

        latest = self.history[-1].get("elapsed_s", 0.0)
        threshold = latest - float(seconds)

        return [
            row for row in self.history
            if row.get("elapsed_s", 0.0) >= threshold
        ]

    def reset_buffer(self):
        """Очистить буфер и историю (кнопка «Сброс»)."""
        self.buffer = []
        self.history.clear()

    # --------------------------------------------------------
    # Экспорт
    # --------------------------------------------------------

    def export_pdf(
            self,
            path,
            graph_pixmap,
            stats_rows,
            table_rows,
            specimen_name=None,
            session_dir=None,
            raw_rows=None,
    ):
        """Экспорт отчёта в PDF (альбомный A4) без новых зависимостей.

        Страница 1: слева — изображение графика, справа — колонка
        карточек, внизу — таблица данных за последние 30 секунд.
        Страница 2: таблица данных графика за последние 60 секунд
        (прореженная до размера страницы).
        """
        writer = QPdfWriter(str(path))
        writer.setPageSize(QPageSize(QPageSize.A4))
        writer.setPageOrientation(QPageLayout.Landscape)
        writer.setResolution(150)

        painter = QPainter()
        if not painter.begin(writer):
            raise RuntimeError("Не удалось начать запись PDF")

        try:
            self._draw_report(
                painter,
                graph_pixmap,
                stats_rows,
                table_rows,
                specimen_name,
                session_dir,
            )

            if raw_rows:
                self._draw_raw_table_page(
                    painter, writer, raw_rows
                )
        finally:
            painter.end()

        logger.info(f"RECORDER: PDF сохранён: {path}")

    def export_csv_tables(
            self,
            path,
            stats_rows,
            table_rows,
            specimen_name=None,
            session_dir=None,
            raw_rows=None,
    ):
        """Экспорт тех же секций в CSV (без изображения)."""
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)

            writer.writerow(["Отчёт по сессии испытания"])
            if specimen_name:
                writer.writerow(["Образец", specimen_name])
            if session_dir:
                writer.writerow(["Папка сессии", str(session_dir)])
            writer.writerow([])

            writer.writerow(["Статистика"])
            for label, value in stats_rows:
                writer.writerow([label, value])
            writer.writerow([])

            writer.writerow(["Данные за последние 30 секунд"])
            writer.writerow(
                ["Время,с", "N цикла", "MAX", "MIN", "MID", "AMP",
                 "Позиция,мм", "Удлинение,мм"]
            )
            for row in table_rows:
                writer.writerow(list(row))

            if raw_rows:
                writer.writerow([])
                writer.writerow(
                    ["Данные графика за последние 60 секунд"]
                )
                writer.writerow(
                    ["Время,с", "RAW", "Фильтр", "Сила", "Позиция,мм"]
                )
                for row in raw_rows:
                    writer.writerow(list(row))

        logger.info(f"RECORDER: CSV сохранён: {path}")

    # --------------------------------------------------------
    # Отрисовка PDF
    # --------------------------------------------------------

    def _draw_report(
            self,
            painter,
            graph_pixmap,
            stats_rows,
            table_rows,
            specimen_name,
            session_dir,
    ):
        painter.setRenderHint(QPainter.Antialiasing, True)

        page = QRectF(painter.viewport())
        margin = 30.0

        text_color = QColor("#111111")
        caption_color = QColor("#666666")
        card_pen = QPen(QColor("#888888"))
        card_brush = QColor("#F2F2F2")

        # --- заголовок ---
        title_font = QFont("Arial", 13)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(text_color)

        title = "Отчёт по сессии испытания"
        if specimen_name:
            title += f" — {specimen_name}"

        painter.drawText(
            QRectF(margin, margin, page.width() - 2 * margin, 26),
            Qt.AlignLeft | Qt.AlignVCenter,
            title,
        )

        content_top = margin + 34.0
        content_bottom = page.height() - margin

        # Раздел отчёта делится на верхнюю часть (график+карточки)
        # и нижнюю (таблица последних 30 секунд).
        table_height = (content_bottom - content_top) * 0.34
        top_height = (content_bottom - content_top) - table_height - 12.0

        graph_width = (page.width() - 2 * margin) * 0.58
        cards_left = margin + graph_width + 14.0
        cards_width = page.width() - margin - cards_left

        # --- слева: график ---
        graph_rect = QRectF(
            margin, content_top, graph_width, top_height
        )
        painter.setPen(card_pen)
        painter.drawRect(graph_rect)

        if graph_pixmap is not None and not graph_pixmap.isNull():
            inner = graph_rect.adjusted(3, 3, -3, -3)
            scaled = graph_pixmap.scaled(
                int(inner.width()),
                int(inner.height()),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            painter.drawPixmap(
                int(inner.x()),
                int(inner.y()),
                scaled,
            )
        else:
            painter.setPen(caption_color)
            painter.drawText(
                graph_rect, Qt.AlignCenter, "График недоступен"
            )

        # --- справа: колонка карточек ---
        painter.setPen(text_color)
        card_count = max(1, len(stats_rows))
        card_gap = 8.0
        card_h = (
            top_height - (card_count - 1) * card_gap
        ) / card_count

        label_font = QFont("Arial", 9)
        value_font = QFont("Arial", 13)
        value_font.setBold(True)

        for index, (label, value) in enumerate(stats_rows):
            card_rect = QRectF(
                cards_left,
                content_top + index * (card_h + card_gap),
                cards_width,
                card_h,
            )
            painter.setPen(card_pen)
            painter.setBrush(card_brush)
            painter.drawRoundedRect(card_rect, 4, 4)

            painter.setBrush(Qt.NoBrush)
            painter.setFont(label_font)
            painter.setPen(caption_color)
            painter.drawText(
                QRectF(
                    card_rect.x() + 8,
                    card_rect.y() + 4,
                    card_rect.width() - 16,
                    16,
                ),
                Qt.AlignLeft | Qt.AlignVCenter,
                str(label),
            )

            painter.setFont(value_font)
            painter.setPen(text_color)
            painter.drawText(
                QRectF(
                    card_rect.x() + 8,
                    card_rect.y() + 20,
                    card_rect.width() - 16,
                    card_rect.height() - 24,
                ),
                Qt.AlignLeft | Qt.AlignVCenter,
                str(value),
            )

        # --- внизу: таблица последних 30 секунд ---
        table_top = content_top + top_height + 12.0
        painter.setFont(label_font)
        painter.setPen(caption_color)
        painter.drawText(
            QRectF(
                margin, table_top - 16, page.width() - 2 * margin, 14
            ),
            Qt.AlignLeft | Qt.AlignVCenter,
            "Данные за последние 30 секунд",
        )

        headers = [
            "Время,с", "N цикла", "MAX", "MIN", "MID", "AMP",
            "Позиция,мм", "Удлинение,мм",
        ]
        col_widths = [0.13, 0.13, 0.12, 0.12, 0.12, 0.12, 0.13, 0.13]
        total_width = page.width() - 2 * margin

        row_height = 15.0
        header_height = 17.0

        table_rect = QRectF(
            margin, table_top, total_width, table_height - 16.0
        )
        painter.setPen(card_pen)
        painter.drawRect(table_rect)

        # Заголовок таблицы.
        x = table_rect.x()
        painter.setFont(label_font)
        painter.setPen(text_color)
        for header, frac in zip(headers, col_widths):
            w = total_width * frac
            painter.drawText(
                QRectF(x + 2, table_rect.y(), w - 4, header_height),
                Qt.AlignLeft | Qt.AlignVCenter,
                header,
            )
            x += w

        y = table_rect.y() + header_height
        painter.setPen(card_pen)
        painter.drawLine(
            int(table_rect.x()),
            int(y),
            int(table_rect.right()),
            int(y),
        )

        # Строки таблицы.
        painter.setFont(label_font)
        painter.setPen(text_color)
        available = table_rect.height() - header_height
        max_rows = max(1, int(available // row_height))

        for row in table_rows[:max_rows]:
            x = table_rect.x()
            for value, frac in zip(row, col_widths):
                w = total_width * frac
                painter.drawText(
                    QRectF(x + 2, y, w - 4, row_height),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    str(value),
                )
                x += w
            y += row_height

    def _draw_raw_table_page(
            self,
            painter,
            writer,
            raw_rows,
    ):
        # Страница 2: данные графика за последние 60 секунд.
        # Полный поток ~330 кадров/с не влезает на страницу —
        # прореживаем равномерно до доступного числа строк.
        writer.newPage()
        painter.setRenderHint(QPainter.Antialiasing, True)

        page = QRectF(painter.viewport())
        margin = 30.0

        text_color = QColor("#111111")
        caption_color = QColor("#666666")
        card_pen = QPen(QColor("#888888"))

        title_font = QFont("Arial", 13)
        title_font.setBold(True)
        label_font = QFont("Arial", 9)

        painter.setFont(title_font)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(margin, margin, page.width() - 2 * margin, 26),
            Qt.AlignLeft | Qt.AlignVCenter,
            "Данные графика за последние 60 секунд",
        )

        total_width = page.width() - 2 * margin
        table_top = margin + 40.0
        table_height = page.height() - table_top - margin

        table_rect = QRectF(
            margin, table_top, total_width, table_height
        )
        painter.setPen(card_pen)
        painter.drawRect(table_rect)

        headers = ["Время,с", "RAW", "Фильтр", "Сила", "Позиция,мм"]
        col_widths = [0.2, 0.2, 0.2, 0.2, 0.2]
        row_height = 15.0
        header_height = 17.0

        x = table_rect.x()
        painter.setFont(label_font)
        painter.setPen(text_color)
        for header, frac in zip(headers, col_widths):
            w = total_width * frac
            painter.drawText(
                QRectF(x + 2, table_rect.y(), w - 4, header_height),
                Qt.AlignLeft | Qt.AlignVCenter,
                header,
            )
            x += w

        y = table_rect.y() + header_height
        painter.setPen(card_pen)
        painter.drawLine(
            int(table_rect.x()), int(y),
            int(table_rect.right()), int(y),
        )

        available = table_rect.height() - header_height
        max_rows = max(1, int(available // row_height))

        rows = list(raw_rows)
        if len(rows) > max_rows:
            # Равномерная прореженность: берём max_rows точек
            # по всему окну (включая последнюю).
            step = len(rows) / max_rows
            rows = [
                rows[int(i * step)]
                for i in range(max_rows)
            ]

        painter.setFont(label_font)
        painter.setPen(text_color)
        for row in rows:
            x = table_rect.x()
            for value, frac in zip(row, col_widths):
                w = total_width * frac
                painter.drawText(
                    QRectF(x + 2, y, w - 4, row_height),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    str(value),
                )
                x += w
            y += row_height
            if y > table_rect.bottom():
                break
