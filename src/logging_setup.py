import logging
import os
from logging.handlers import RotatingFileHandler

from src.protocol import REPO_ROOT

logger = logging.getLogger("fatigue")

# Отдельный логгер FFT-фильтра отображения: ротируемый файл
# logs/fft_filter.log — построчные метрики пересчёта (режим, fs,
# размер, RMS вход/выход, подавление, время) для оценки работы
# фильтра на стенде. Создаётся лениво при первом обращении —
# fft_filter импортируется до setup_logging.
fft_logger = logging.getLogger("fatigue.fft")
fft_logger.setLevel(logging.DEBUG)
fft_logger.propagate = False


def _ensure_fft_log_handler():
    LOG_DIR = REPO_ROOT / "logs"
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        filename=str(LOG_DIR / "fft_filter.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        )
    )
    fft_logger.addHandler(handler)


def setup_logging():
    """Configure the 'fatigue' logger with console and rotating file handlers."""
    logger.setLevel(logging.DEBUG)

    # Ensure log directory exists (anchored to the repo root)
    LOG_DIR = REPO_ROOT / "logs"
    os.makedirs(LOG_DIR, exist_ok=True)

    # Console handler — INFO level
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Rotating file handler — DEBUG level, 5 MB x3, utf-8
    file_handler = RotatingFileHandler(
        filename=str(LOG_DIR / "app_debug.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)

    # Common format
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)

    # Remove any existing handlers to avoid duplicates
    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)