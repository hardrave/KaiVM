import time
from pathlib import Path
from typing import Tuple, Optional
from io import BytesIO

from kaivm.hid.mouse import AbsoluteMouseHID
from kaivm.util.log import get_logger
from kaivm.util.paths import LATEST_JPG, CALIBRATION_FILE, CONFIG_DIR

log = get_logger("kaivm.calibrate")

try:
    from PIL import Image, ImageChops
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

def _wait_for_new_frame(path: Path, last_mtime: float, timeout: float = 2.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = path.stat()
            if st.st_mtime > last_mtime:
                return True
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    return False

def _read_img(path: Path) -> Tuple[Optional[Image.Image], float]:
    if not path.exists():
        return None, 0.0
    st = path.stat()
    try:
        img = Image.open(BytesIO(path.read_bytes())).convert("RGB")
        return img, st.st_mtime
    except Exception:
        return None, st.st_mtime

def _find_cursor_pos(mouse: AbsoluteMouseHID, target_x: int, target_y: int, label: str) -> Optional[Tuple[int, int]]:
    log.info(f"Sampling {label}...")
    
    # 1. Move AWAY to clear the spot (ensure visual change when we arrive)
    if abs(target_x - 16383) < 1000 and abs(target_y - 16383) < 1000:
        mouse.move(0, 0)
    else:
        mouse.move(16383, 16383)
        
    time.sleep(0.5)
    _, mtime = _read_img(LATEST_JPG)
    
    # 2. Move TO target
    mouse.move(target_x, target_y)
    _wait_for_new_frame(LATEST_JPG, mtime)
    time.sleep(0.5) # Settle
    
    target_img, mtime = _read_img(LATEST_JPG)
    if not target_img: return None
    
    # 3. Wiggle to localize
    # Move slightly to generate a local diff
    wiggle_offset = 500 if target_x < 30000 else -500
    mouse.move(target_x + wiggle_offset, target_y)
    _wait_for_new_frame(LATEST_JPG, mtime)
    time.sleep(0.3)
    wiggle_img, _ = _read_img(LATEST_JPG)
    
    diff = ImageChops.difference(target_img, wiggle_img).convert("L")
    diff = diff.point(lambda x: 255 if x > 20 else 0)
    bbox = diff.getbbox()
    
    if not bbox:
        log.warning(f"No cursor detected at {label}")
        return None
        
    l, t, r, b = bbox
    cx = (l + r) // 2
    cy = (t + b) // 2
    log.info(f"  -> Found at ({cx}, {cy})")
    return cx, cy

def calibrate_mouse_auto(mouse: AbsoluteMouseHID) -> str:
    if not HAS_PIL:
        raise RuntimeError("PIL not installed, cannot calibrate.")
    
    if not LATEST_JPG.exists():
        raise RuntimeError(f"Capture not running? {LATEST_JPG} missing.")

    log.info("Starting robust mouse calibration (Inset method)...")
    
    MAX = 32767
    
    # Use inset points to avoid edge clamping/overscan issues
    # and to better estimate the effective slope in the usable area.
    P1_LOG = 0.125
    P2_LOG = 0.875
    
    hid_p1 = int(P1_LOG * MAX)
    hid_p2 = int(P2_LOG * MAX)
    
    # 1. P1 (Top-Left inset)
    pos_1 = _find_cursor_pos(mouse, hid_p1, hid_p1, "P1 (Top-Left)")
    if not pos_1: raise RuntimeError("Failed to detect P1")
    
    # 2. P2 (Bottom-Right inset)
    pos_2 = _find_cursor_pos(mouse, hid_p2, hid_p2, "P2 (Bottom-Right)")
    if not pos_2: raise RuntimeError("Failed to detect P2")
    
    img, _ = _read_img(LATEST_JPG)
    w, h = img.size
    
    x1, y1 = pos_1
    x2, y2 = pos_2
    
    # Normalize visual coordinates
    nx1 = x1 / w
    ny1 = y1 / h
    nx2 = x2 / w
    ny2 = y2 / h
    
    log.info(f"Detected P1: ({x1}, {y1}) -> norm({nx1:.4f}, {ny1:.4f})")
    log.info(f"Detected P2: ({x2}, {y2}) -> norm({nx2:.4f}, {ny2:.4f})")
    
    # Safety check
    if nx2 <= nx1 or ny2 <= ny1:
        raise RuntimeError(f"Invalid detection: P1=({x1},{y1}) P2=({x2},{y2})")
        
    # Calculate linear mapping: Logical = Visual * Scale + Offset
    # We have two points:
    # P1_LOG = nx1 * Scale + Offset
    # P2_LOG = nx2 * Scale + Offset
    # -> Scale = (P2_LOG - P1_LOG) / (nx2 - nx1)
    # -> Offset = P1_LOG - (nx1 * Scale)
    
    log_range = P2_LOG - P1_LOG
    
    scale_x = log_range / (nx2 - nx1)
    scale_y = log_range / (ny2 - ny1)
    
    offset_x = P1_LOG - (nx1 * scale_x)
    offset_y = P1_LOG - (ny1 * scale_y)
    
    res = f"{scale_x:.4f},{scale_y:.4f},{offset_x:.4f},{offset_y:.4f}"
    log.info(f"Calibration result: {res}")
    
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CALIBRATION_FILE.write_text(res)
        log.info(f"Saved calibration to {CALIBRATION_FILE}")
    except Exception as e:
        log.error(f"Failed to save calibration: {e}")
        
    return res
