import sys

from PyQt5.QtWidgets import QApplication

from graph_widget import ForceGraphWidget  # noqa: F401  (re-export)
from calibration_window import CalibrationWindow


def main():
    app = QApplication(sys.argv)

    app.setStyle(
        "Fusion"
    )

    window = CalibrationWindow()

    window.show()

    sys.exit(
        app.exec_()
    )


if __name__ == "__main__":
    main()
