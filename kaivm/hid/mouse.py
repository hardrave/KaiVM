from __future__ import annotations

import os
import time
from dataclasses import dataclass

from kaivm.hid.udc import wait_udc_configured
from kaivm.util.log import get_logger

log = get_logger("kaivm.hid.mouse")

# 3-byte boot mouse: [buttons, dx, dy]
REPORT_LEN = 3
BUTTONS = {"left": 1, "right": 2, "middle": 4}


def _to_i8(x: int) -> int:
    # clamp to signed int8
    if x > 127:
        x = 127
    if x < -127:
        x = -127
    return x & 0xFF


def _pack(btn: int, dx: int, dy: int) -> bytes:
    return bytes([btn & 0xFF, _to_i8(dx), _to_i8(dy)])


@dataclass
class MouseHID:
    dev: str = "/dev/hidg1"
    io_timeout: float = 5.0

    def _write_with_retry(self, payload: bytes, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            fd = os.open(self.dev, os.O_WRONLY | os.O_NONBLOCK)
            try:
                while time.time() < end:
                    try:
                        os.write(fd, payload)
                        return True
                    except BlockingIOError:
                        time.sleep(0.01)
                    except BrokenPipeError:
                        time.sleep(0.1)
                        break
            finally:
                os.close(fd)
        return False

    def move(self, dx: int, dy: int) -> None:
        if not wait_udc_configured(timeout=20.0):
            raise RuntimeError("UDC not configured (host not enumerated?)")

        if not self._write_with_retry(_pack(0, dx, dy), timeout=self.io_timeout):
            raise TimeoutError("mouse move timed out")

    def click(self, button: str = "left", hold_ms: int = 60) -> None:
        if button not in BUTTONS:
            raise ValueError(f"Unknown button: {button}")

        if not wait_udc_configured(timeout=20.0):
            raise RuntimeError("UDC not configured (host not enumerated?)")

        mask = BUTTONS[button]
        if not self._write_with_retry(_pack(mask, 0, 0), timeout=self.io_timeout):
            raise TimeoutError("mouse click down timed out")
        time.sleep(hold_ms / 1000.0)
        if not self._write_with_retry(_pack(0, 0, 0), timeout=self.io_timeout):
            raise TimeoutError("mouse click up timed out")

