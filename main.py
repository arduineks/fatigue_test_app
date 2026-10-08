import sys

from PyQt5.QtWidgets import QApplication

from src.graph_widget import ForceGraphWidget  # noqa: F401  (re-export)
from src.main_window import MainWindow
from src.logging_setup import setup_logging, logger


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
