import sys

from PyQt5.QtWidgets import QApplication

from graph_widget import ForceGraphWidget  # noqa: F401  (re-export)
from main_window import MainWindow
from logging_setup import setup_logging, logger


def main():
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
