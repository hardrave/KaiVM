from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from kaivm.util.log import get_logger

log = get_logger("kaivm.hid.udc")

UDC_CLASS = Path("/sys/class/udc")
GADGET_UDC_PATH = Path("/sys/kernel/config/usb_gadget/kaivm/UDC")


def udc_name() -> str:
    # most Pi setups expose exactly one
    return next(UDC_CLASS.iterdir()).name


def udc_state(name: Optional[str] = None) -> str:
    if name is None:
        name = udc_name()
    return (UDC_CLASS / name / "state").read_text().strip()


def wait_udc_configured(timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if udc_state() == "configured":
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def usb_replug(gadget_udc_path: Path = GADGET_UDC_PATH, settle: float = 1.0) -> None:
    """
    Soft re-enumerate: unbind/rebind without removing power.

    Requires root (writes to configfs). For PoC robustness:
    - If not root, we skip with a warning (no crash).
    """
    if os.geteuid() != 0:
        log.warning("usb_replug requires root; skipping (run `sudo -E kaivm ...` if you want it)")
        return

    u = udc_name()
    log.info("USB soft replug: unbind -> bind (%s)", u)

    try:
        gadget_udc_path.write_text("")
    except PermissionError:
        log.warning("Permission denied writing %s (are you root?)", gadget_udc_path)
        return
    except Exception as e:
        log.warning("Failed to unbind UDC (%s): %s", gadget_udc_path, e)

    time.sleep(settle)

    try:
        gadget_udc_path.write_text(u)
    except PermissionError:
        log.warning("Permission denied writing %s (are you root?)", gadget_udc_path)
        return
    except Exception as e:
        log.warning("Failed to bind UDC (%s): %s", gadget_udc_path, e)

    time.sleep(settle)

