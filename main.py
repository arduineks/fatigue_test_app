import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from src.graph_widget import ForceGraphWidget  # noqa: F401  (re-export)
from src.main_window import MainWindow
from src.logging_setup import setup_logging, logger


def main():
    # High-DPI: атрибуты выставляются ДО создания QApplication.
    # На Windows 10 при масштабе 125–150 % без них окно рендерится
    # с растяжением/дрожанием (мыло и рывки при перетаскивании).
    QApplication.setAttribute(
        Qt.AA_EnableHighDpiScaling, True
    )
    QApplication.setAttribute(
        Qt.AA_UseHighDpiPixmaps, True
    )

    app = QApplication(sys.argv)

    setup_logging()
    logger.info("Application started")

    app.setStyle(
        "Fusion"
    )

    window = MainWindow()

    window.show()

    sys.exit(
        app.exec_()
    )


if __name__ == "__main__":
    main()
