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

MEASUREMENT_FRAME_FORMAT = "<BiiffB"
MEASUREMENT_FRAME_SIZE = struct.calcsize(
    MEASUREMENT_FRAME_FORMAT
)

FRAME_START = 0xAA
FRAME_END = 0xBB

GRAVITY = 0.00980665

INI_PATH = Path(__file__).resolve().with_name(
    "calibration.ini"
)