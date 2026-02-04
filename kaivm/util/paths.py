from pathlib import Path

RUN_DIR = Path("/run/kaivm")
LATEST_JPG = RUN_DIR / "latest.jpg"
LIVE_MJPG = RUN_DIR / "live.mjpg"

STOP_FILE = Path("/tmp/kaivm.stop")

CONFIG_DIR = Path.home() / ".config" / "kaivm"
CALIBRATION_FILE = CONFIG_DIR / "calibration.txt"

