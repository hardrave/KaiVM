from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, Tuple

from kaivm.hid.udc import wait_udc_configured
from kaivm.util.log import get_logger

log = get_logger("kaivm.hid.keyboard")

# Boot keyboard: 8 bytes [mods, 0, k1,k2,k3,k4,k5,k6]
REPORT_LEN = 8

# Modifier bits (USB HID boot keyboard)
MOD_LCTRL  = 0x01
MOD_LSHIFT = 0x02
MOD_LALT   = 0x04
MOD_LGUI   = 0x08  # Windows / Command / Super

# Simple ASCII map -> (modifier, keycode)
ASCII_MAP: Dict[str, Tuple[int, int]] = {}

def _add(ch: str, mod: int, code: int) -> None:
    ASCII_MAP[ch] = (mod, code)

# a-z
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _add(ch, 0, 0x04 + i)
# A-Z
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _add(ch, MOD_LSHIFT, 0x04 + i)

# digits and shifted digits
digits = "1234567890"
shifted = "!@#$%^&*()"
for i, (d, s) in enumerate(zip(digits, shifted)):
    code = 0x1E + i
    _add(d, 0, code)
    _add(s, MOD_LSHIFT, code)

# whitespace / controls
_add(" ", 0, 0x2C)
_add("\n", 0, 0x28)
_add("\t", 0, 0x2B)

# punctuation
punct = {
    "-": (0, 0x2D), "_": (MOD_LSHIFT, 0x2D),
    "=": (0, 0x2E), "+": (MOD_LSHIFT, 0x2E),
    "[": (0, 0x2F), "{": (MOD_LSHIFT, 0x2F),
    "]": (0, 0x30), "}": (MOD_LSHIFT, 0x30),
    "\\": (0, 0x31), "|": (MOD_LSHIFT, 0x31),
    ";": (0, 0x33), ":": (MOD_LSHIFT, 0x33),
    "'": (0, 0x34), "\"": (MOD_LSHIFT, 0x34),
    "`": (0, 0x35), "~": (MOD_LSHIFT, 0x35),
    ",": (0, 0x36), "<": (MOD_LSHIFT, 0x36),
    ".": (0, 0x37), ">": (MOD_LSHIFT, 0x37),
    "/": (0, 0x38), "?": (MOD_LSHIFT, 0x38),
}
for k, (m, c) in punct.items():
    _add(k, m, c)

# Named keys / common special keys
KEYCODES: Dict[str, int] = {
    "ENTER": 0x28,
    "ESC": 0x29,
    "ESCAPE": 0x29,
    "BACKSPACE": 0x2A,
    "TAB": 0x2B,
    "SPACE": 0x2C,
    "CAPSLOCK": 0x39,
    "LEFT": 0x50,
    "RIGHT": 0x4F,
    "UP": 0x52,
    "DOWN": 0x51,
    "DELETE": 0x4C,
    "HOME": 0x4A,
    "END": 0x4D,
    "PAGEUP": 0x4B,
    "PAGEDOWN": 0x4E,
}

# Function keys F1..F12
for i in range(1, 13):
    KEYCODES[f"F{i}"] = 0x3A + (i - 1)

MOD_NAMES = {
    "CTRL": MOD_LCTRL,
    "CONTROL": MOD_LCTRL,
    "SHIFT": MOD_LSHIFT,
    "ALT": MOD_LALT,
    # GUI / Windows / Command / Super / Meta
    "GUI": MOD_LGUI,
    "WIN": MOD_LGUI,
    "WINDOWS": MOD_LGUI,
    "CMD": MOD_LGUI,
    "COMMAND": MOD_LGUI,
    "SUPER": MOD_LGUI,
    "META": MOD_LGUI,
}

def _pack_report(mod: int, key: int) -> bytes:
    return bytes([mod & 0xFF, 0x00, key & 0xFF, 0, 0, 0, 0, 0])


@dataclass
class KeyboardHID:
    dev: str = "/dev/hidg0"
    io_timeout: float = 5.0

    def _write_with_retry(self, payload: bytes, timeout: float) -> bool:
        """
        Nonblocking open+write with retry for EAGAIN; reopen on BrokenPipe.
        """
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

    def send_key(self, mod: int, keycode: int, hold_ms: int = 30) -> None:
        if not wait_udc_configured(timeout=20.0):
            raise RuntimeError("UDC not configured (host not enumerated?)")

        down = _pack_report(mod, keycode)
        up = _pack_report(0, 0)

        if not self._write_with_retry(down, timeout=self.io_timeout):
            raise TimeoutError(f"keyboard write timeout (down) dev={self.dev}")
        time.sleep(hold_ms / 1000.0)
        if not self._write_with_retry(up, timeout=self.io_timeout):
            raise TimeoutError(f"keyboard write timeout (up) dev={self.dev}")

    def send_keycode(self, mod: int, key: int, hold_ms: int = 30) -> None:
        self.send_key(mod, key, hold_ms=hold_ms)

    def send_text(self, text: str, inter_key_ms: int = 20) -> None:
        for ch in text:
            if ch not in ASCII_MAP:
                log.warning("No key mapping for %r; skipping", ch)
                continue
            mod, code = ASCII_MAP[ch]
            self.send_key(mod, code)
            time.sleep(inter_key_ms / 1000.0)

    def send_hotkey(self, combo: str) -> bool:
        """
        Accepts:
          - "command+space"
          - "ctrl+l"
          - "alt+f4"
          - "gui+tab"
          - "shift+ENTER"
        Returns True if sent; False if unknown.
        """
        raw = (combo or "").strip()
        if not raw:
            return False

        parts = [p.strip() for p in raw.replace("-", "+").split("+") if p.strip()]
        if not parts:
            return False

        mod = 0
        key_part = None

        for p in parts:
            up = p.upper()

            # Special case: some models emit a literal space as the key name
            if p == " ":
                up = "SPACE"

            if up in MOD_NAMES:
                mod |= MOD_NAMES[up]
                continue

            # Allow single character final key, like "l" in ctrl+l
            if len(p) == 1 and p in ASCII_MAP:
                m2, kc = ASCII_MAP[p]
                # if ASCII requires shift (e.g. "?"), fold it into modifiers
                mod |= m2
                key_part = kc
                continue

            if up in KEYCODES:
                key_part = KEYCODES[up]
                continue

            # allow "SPACE" in lower-case etc
            if up == " ":
                key_part = KEYCODES["SPACE"]
                continue

            return False

        if key_part is None:
            # A modifier alone does nothing useful; refuse
            return False

        self.send_key(mod, key_part)
        return True

