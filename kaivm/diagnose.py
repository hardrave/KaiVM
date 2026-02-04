import time
from pathlib import Path
from typing import Tuple, Optional, List
from io import BytesIO

from kaivm.hid.mouse import AbsoluteMouseHID
from kaivm.util.log import get_logger
from kaivm.util.paths import LATEST_JPG

log = get_logger("kaivm.diagnose")

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

def _find_cursor_hotspot(mouse: AbsoluteMouseHID, target_x: int, target_y: int, label: str) -> Optional[Tuple[int, int]]:

    log.info(f"Testing {label} (HID: {target_x}, {target_y})...")

    

    # 1. Move to Center (to clear the target area if we are testing corners)

    # 16383 is center.

    mouse.move(16383, 16383)

    time.sleep(0.5)

    base_img, mtime = _read_img(LATEST_JPG)

    

    # 2. Move TO Target

    mouse.move(target_x, target_y)

    _wait_for_new_frame(LATEST_JPG, mtime)

    time.sleep(0.8) # Settle

    

    target_img, _ = _read_img(LATEST_JPG)

    if not target_img or not base_img: return None

    

    # 3. Diff

    # The diff will show the cursor at Target AND the cursor at Center.

    # We need to isolate the one at Target.

    # If testing TL (0,0), look at top-left quadrant.

    # If testing BR, look at bottom-right.

    

    diff = ImageChops.difference(base_img, target_img).convert("L")

    diff = diff.point(lambda x: 255 if x > 20 else 0)

    

    # Crop to expected area to exclude the "Center" ghost

    w, h = diff.size

    

    # Define search region based on label

    region = None

    if "Top-Left" in label:

        region = (0, 0, w//2, h//2)

    elif "Top-Right" in label:

        region = (w//2, 0, w, h//2)

    elif "Bottom-Left" in label:

        region = (0, h//2, w//2, h)

    elif "Bottom-Right" in label:

        region = (w//2, h//2, w, h)

    elif "Center" in label:

        # For center, we need to move AWAY to a corner to avoid overlap?

        # Let's handle center separately or skip.

        # If we moved start to TL, then Center blob is in center.

        # But we moved start to Center... wait.

        pass

        

    if "Center" in label:

        # Special case: Move to TL first.

        mouse.move(0, 0)

        time.sleep(0.5)

        base_img, mtime = _read_img(LATEST_JPG)

        mouse.move(target_x, target_y)

        _wait_for_new_frame(LATEST_JPG, mtime)

        time.sleep(0.5)

        target_img, _ = _read_img(LATEST_JPG)

        diff = ImageChops.difference(base_img, target_img).convert("L")

        diff = diff.point(lambda x: 255 if x > 20 else 0)

        region = (w//4, h//4, 3*w//4, 3*h//4)



    if region:

        diff = diff.crop(region)

    

    bbox = diff.getbbox()

    if not bbox:

        log.warning(f"No cursor detected for {label}")

        return None

    

    # bbox is relative to crop. Add offset.

    rx, ry, _, _ = region if region else (0,0,0,0)

    l, t, r, b = bbox

    

    # Hotspot detection:

    # For standard arrow, hotspot is Top-Left of the image.

    final_x = rx + l

    final_y = ry + t

    

    log.info(f"  -> Hotspot at ({final_x}, {final_y})")

    return final_x, final_y



def diagnose_mouse(mouse: AbsoluteMouseHID):



    if not HAS_PIL:



        print("PIL missing.")



        return



        



    points = [



        ("Top-Left", 1000, 1000),



        ("Top-Right", 31767, 1000),



        ("Bottom-Left", 1000, 31767),



        ("Bottom-Right", 31767, 31767),



    ]



    



    img, _ = _read_img(LATEST_JPG)



    w, h = (1280, 720)



    if img: w, h = img.size



    



    print(f"Image Size: {w}x{h}")

    print(f"{'Label':<15} | {'HID (X,Y)':<15} | {'Detected':<15} | {'Norm':<15}")

    print("-" * 70)

    

    results = []

    for label, hx, hy in points:

        pos = _find_cursor_hotspot(mouse, hx, hy, label)

        if pos:

            px, py = pos

            nx = px / w

            ny = py / h

            print(f"{label:<15} | {hx:<5},{hy:<5}    | {px:<5},{py:<5}    | {nx:.3f},{ny:.3f}")

            results.append((label, px, py))

        else:

            print(f"{label:<15} | {hx:<5},{hy:<5}    | FAIL            | -")

            

    # Auto-calc

    if len(results) == 4:

        # Order: TL, TR, BL, BR

        tl = results[0] # (label, x, y)

        br = results[3]

        

        min_x = tl[1]

        min_y = tl[2]

        max_x = br[1]

        max_y = br[2]

        

        scale_x = 1.0 / ((max_x - min_x) / w)

        scale_y = 1.0 / ((max_y - min_y) / h)

        

        offset_x = -(min_x / w) * scale_x

        offset_y = -(min_y / h) * scale_y

        

        print("\nSuggested Calibration:")

        print(f"Scale:  {scale_x:.4f}, {scale_y:.4f}")

        print(f"Offset: {offset_x:.4f}, {offset_y:.4f}")



if __name__ == "__main__":
    from kaivm.util.log import setup_logging
    setup_logging(verbose=True)
    m = AbsoluteMouseHID()
    diagnose_mouse(m)
