import logging
import os
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("fatigue")


def setup_logging():
    """Configure the 'fatigue' logger with console and rotating file handlers."""
    logger.setLevel(logging.DEBUG)

    # Ensure log directory exists
    os.makedirs("logs", exist_ok=True)

    # Console handler — INFO level
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Rotating file handler — DEBUG level, 5 MB x3, utf-8
    file_handler = RotatingFileHandler(
        filename="logs/app_debug.log",
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