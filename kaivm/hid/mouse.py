from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

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
    _fd: Optional[int] = field(default=None, init=False, repr=False)

    def _get_fd(self) -> int:
        if self._fd is None:
            self._fd = os.open(self.dev, os.O_WRONLY | os.O_NONBLOCK)
        return self._fd

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _write_with_retry(self, payload: bytes) -> None:
        end = time.time() + self.io_timeout
        while True:
            try:
                fd = self._get_fd()
                os.write(fd, payload)
                return
            except BlockingIOError:
                if time.time() >= end:
                    raise TimeoutError("mouse write timed out (blocking)")
                time.sleep(0.001)
            except (BrokenPipeError, OSError) as e:
                self._close_fd()
                if time.time() >= end:
                    raise e
                # Maybe UDC is reconfiguring
                time.sleep(0.05)

    def send_report(self, buttons: int, dx: int, dy: int) -> None:
        # We don't check wait_udc_configured() every time anymore.
        # It's too slow (file read). We rely on write() failing if not ready.
        self._write_with_retry(_pack(buttons, dx, dy))

    def move(self, dx: int, dy: int) -> None:
        # Break down large moves into chunks
        while dx != 0 or dy != 0:
            step_x = max(-127, min(127, dx))
            step_y = max(-127, min(127, dy))
            
            self.send_report(0, step_x, step_y)
            
            dx -= step_x
            dy -= step_y
            
            # Small delay to let the host process the reports if we are sending many
            # With kept-open FD, we might flood the host, so keep a tiny sleep but smaller.
            if dx != 0 or dy != 0:
                time.sleep(0.002)

    def click(self, button: str = "left", hold_ms: int = 60) -> None:
        if button not in BUTTONS:
            raise ValueError(f"Unknown button: {button}")

        mask = BUTTONS[button]
        self.send_report(mask, 0, 0)
        time.sleep(hold_ms / 1000.0)
        self.send_report(0, 0, 0)
    
    def __del__(self) -> None:
        self._close_fd()


@dataclass
class AbsoluteMouseHID:
    dev: str = "/dev/hidg2"
    io_timeout: float = 5.0
    _fd: Optional[int] = field(default=None, init=False, repr=False)

    def _get_fd(self) -> int:
        if self._fd is None:
            self._fd = os.open(self.dev, os.O_WRONLY | os.O_NONBLOCK)
        return self._fd

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _write_with_retry(self, payload: bytes) -> None:
        end = time.time() + self.io_timeout
        while True:
            try:
                fd = self._get_fd()
                os.write(fd, payload)
                return
            except BlockingIOError:
                if time.time() >= end:
                    raise TimeoutError("abs mouse write timed out (blocking)")
                time.sleep(0.001)
            except (BrokenPipeError, OSError) as e:
                self._close_fd()
                if time.time() >= end:
                    raise e
                time.sleep(0.05)

    def send_report(self, buttons: int, x: int, y: int) -> None:
        """
        x, y: 0..32767
        """
        # Clamp
        x = max(0, min(32767, x))
        y = max(0, min(32767, y))
        
        # Format: [buttons, x_lo, x_hi, y_lo, y_hi]
        payload = bytes([
            buttons & 0xFF,
            x & 0xFF, (x >> 8) & 0xFF,
            y & 0xFF, (y >> 8) & 0xFF
        ])
        
        self._write_with_retry(payload)

    def move(self, x: int, y: int) -> None:
        self.send_report(0, x, y)

    def click(self, x: int, y: int, button: str = "left", hold_ms: int = 60) -> None:
        if button not in BUTTONS:
            raise ValueError(f"Unknown button: {button}")

        mask = BUTTONS[button]
        # Move first (ensure cursor is there)
        self.send_report(0, x, y)
        time.sleep(0.05)
        # Press
        self.send_report(mask, x, y)
        time.sleep(hold_ms / 1000.0)
        # Release (keep pos)
        self.send_report(0, x, y)

    def __del__(self) -> None:
        self._close_fd()

