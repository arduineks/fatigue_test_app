import struct
from pathlib import Path

# ============================================================
# CONSTANTS
# ============================================================

SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.0

HELLO_COMMAND = "HELLO_YYY"

CAL_ZERO_COMMAND = "CALZERO_YYY"
CAL_LOAD_COMMAND = "CALLOAD_YYY"
CAL_GET_COMMAND = "CALGET_YYY"

CAL_APPLY_COMMAND = "CALAPPLY_YYY"

START_COMMAND = "START_YYY"
STOP_COMMAND = "STOP_YYY"

# Калибровка траверсы (возврат в ноль/домашнюю позицию).
HOME_COMMAND = "HOMEFORCE_0.1_0.1_5_YYY"

MEASUREMENT_FRAME_FORMAT = "<BiiffB"
MEASUREMENT_FRAME_SIZE = struct.calcsize(
    MEASUREMENT_FRAME_FORMAT
)

FRAME_START = 0xAA
FRAME_END = 0xBB

GRAVITY = 0.00980665

# Корневая папка репозитория (для путей по умолчанию).
# Корень «установки» приложения: рядом с конфигами, логами и
# «Saved data». В PyInstaller-сборке (frozen) файлы пакета лежат
# в _internal — корнем считаем папку с exe-файлом; в обычном
# запуске — родитель папки src/.
import sys

if getattr(sys, "frozen", False):
    REPO_ROOT = Path(sys.executable).resolve().parent
else:
    REPO_ROOT = Path(__file__).resolve().parent.parent

INI_PATH = REPO_ROOT / "configs" / "calibration.ini"

# Отдельный файл настроек приложения (не калибровки):
# имя образца, интервал записи, путь сохранения сессий.
APP_SETTINGS_PATH = REPO_ROOT / "configs" / "app_settings.ini"