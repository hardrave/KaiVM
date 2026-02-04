from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

ALLOWED_TYPES = {"wait", "mouse_move", "mouse_click", "type_text", "key", "done"}
ALLOWED_BUTTONS = {"left", "right", "middle"}

DANGER_PATTERNS = [
    "rm -", "rm -rf", "del /", "format ", "mkfs", "shutdown", "reboot",
    "passwd", "net user", "reg delete", "diskpart", "bcdedit",
]


@dataclass
class Action:
    type: str
    ms: Optional[int] = None
    dx: Optional[int] = None
    dy: Optional[int] = None
    x: Optional[int] = None
    y: Optional[int] = None
    button: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None
    summary: Optional[str] = None


def _i8(x: int) -> int:
    if x < -4096 or x > 4096:
        raise ValueError("dx/dy must be in [-4096,4096]")
    return x


def parse_plan(plan: Dict[str, Any]) -> List[Action]:
    if not isinstance(plan, dict):
        raise ValueError("plan must be object")
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be non-empty array")

    out: List[Action] = []
    for a in actions:
        if not isinstance(a, dict):
            raise ValueError("each action must be object")
        t = a.get("type")
        if t not in ALLOWED_TYPES:
            raise ValueError(f"unsupported action type: {t}")

        act = Action(type=t)

        if t == "wait":
            ms = int(a.get("ms", 0))
            if ms < 0 or ms > 60000:
                raise ValueError("wait.ms out of range")
            act.ms = ms

        elif t == "mouse_move":
            if "dx" in a or "dy" in a:
                act.dx = _i8(int(a.get("dx", 0)))
                act.dy = _i8(int(a.get("dy", 0)))
            if "x" in a or "y" in a:
                # Absolute coordinates (pixels)
                act.x = int(a.get("x", 0))
                act.y = int(a.get("y", 0))

        elif t == "mouse_click":
            btn = a.get("button", "left")
            if btn not in ALLOWED_BUTTONS:
                raise ValueError("mouse_click.button invalid")
            act.button = btn
            if "x" in a or "y" in a:
                act.x = int(a.get("x", 0))
                act.y = int(a.get("y", 0))

        elif t == "type_text":
            text = a.get("text", "")
            if not isinstance(text, str) or len(text) > 2000:
                raise ValueError("type_text.text invalid")
            act.text = text

        elif t == "key":
            k = a.get("key", "")
            if not isinstance(k, str) or len(k) > 64:
                raise ValueError("key.key invalid")
            act.key = k

        elif t == "done":
            s = a.get("summary", "")
            if not isinstance(s, str):
                s = str(s)
            act.summary = s

        out.append(act)

    return out


def is_dangerous_text(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in DANGER_PATTERNS)

