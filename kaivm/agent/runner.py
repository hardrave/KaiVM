from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

from kaivm.agent.validate import Action, is_dangerous_text, parse_plan
from kaivm.gemini.client import GeminiPlanner
from kaivm.hid.keyboard import KeyboardHID
from kaivm.hid.mouse import MouseHID, AbsoluteMouseHID
from kaivm.hid.udc import usb_replug
from kaivm.util.image import get_image_size
from kaivm.util.log import get_logger
from kaivm.util.paths import LATEST_JPG, STOP_FILE, RUN_DIR

log = get_logger("kaivm.agent")


@dataclass
class AgentConfig:
    latest_jpg: Path = LATEST_JPG
    max_steps: int = 30
    step_sleep: float = 0.15
    overall_timeout_s: float = 3600.0
    dry_run: bool = False
    confirm: bool = False
    allow_danger: bool = False
    do_replug: bool = False
    dump_last_sent: bool = True

    # Closed-loop controls
    max_actions_per_step: int = 5
    pre_plan_frame_timeout_s: float = 1.2
    post_action_frame_timeout_s: float = 2.8
    frame_poll_s: float = 0.05

    # Smart waits injected by runner (ms)
    type_to_enter_wait_ms: int = 50
    app_launch_settle_ms: int = 1000
    search_submit_settle_ms: int = 1500

    # Prevent premature termination
    min_steps_before_done: int = 2

    # Mouse calibration (normalized 0.0-1.0)
    # hid = img * scale + offset
    cal_x_scale: float = 1.0
    cal_y_scale: float = 1.0
    cal_x_offset: float = 0.3587
    cal_y_offset: float = 0.3547


KEY_ALIASES = {
    "ENTER": ("\n",),
    "TAB": ("\t",),
    "SPACE": (" ",),
}


def _confirm_batch(actions: List[Action]) -> bool:
    print("\nPlanned actions:")
    for a in actions:
        print(" -", a)
    ans = input("Execute? [y/N] ").strip().lower()
    return ans in ("y", "yes")


def _actions_brief(actions: List[Action]) -> str:
    out: List[str] = []
    for a in actions:
        if a.type == "key":
            out.append(f"key({a.key})")
        elif a.type == "type_text":
            t = a.text or ""
            out.append(f"type_text({t[:40]!r}{'…' if len(t) > 40 else ''})")
        elif a.type == "mouse_move":
            if a.x is not None and a.y is not None:
                out.append(f"mouse_move(@{a.x},{a.y})")
            else:
                out.append(f"mouse_move({a.dx},{a.dy})")
        elif a.type == "mouse_click":
            if a.x is not None and a.y is not None:
                out.append(f"mouse_click({a.button}@{a.x},{a.y})")
            else:
                out.append(f"mouse_click({a.button})")
        elif a.type == "wait":
            out.append(f"wait({a.ms}ms)")
        elif a.type == "done":
            out.append("done")
        else:
            out.append(a.type)
    return "; ".join(out)


def _wait_for_new_frame(path: Path, last_mtime: float, timeout_s: float, poll_s: float) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            st = path.stat()
            if st.st_mtime > last_mtime:
                return True
        except FileNotFoundError:
            pass
        time.sleep(poll_s)
    return False


def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _normalize_actions(actions: List[Action]) -> List[Action]:
    """
    Fix common LLM patterns / paper cuts.

    - modifier then space -> modifier+space
    - modifier alone followed by typing -> treat as launcher open (modifier+space heuristic)
    """
    out: List[Action] = []
    i = 0

    def _is_cmd_like(s: str) -> bool:
        return (s or "").strip().lower() in ("command", "cmd", "gui", "win", "super", "meta")

    while i < len(actions):
        a = actions[i]

        if (
            a.type == "key"
            and _is_cmd_like(a.key or "")
            and i + 1 < len(actions)
            and actions[i + 1].type == "key"
        ):
            b = actions[i + 1]
            bkey = (b.key or "")
            if bkey == " " or bkey.strip().lower() in ("space",):
                out.append(Action(type="key", key=f"{a.key}+space"))
                i += 2
                continue

        if a.type == "key" and _is_cmd_like(a.key or ""):
            if i + 1 < len(actions) and actions[i + 1].type == "type_text":
                out.append(Action(type="key", key=f"{a.key}+space"))
                i += 1
                continue
            if (
                i + 2 < len(actions)
                and actions[i + 1].type == "wait"
                and actions[i + 2].type == "type_text"
            ):
                out.append(Action(type="key", key=f"{a.key}+space"))
                i += 1
                continue

        out.append(a)
        i += 1

    return out


def _is_launcher_key(key: str) -> bool:
    k = (key or "").strip().lower()
    return k in ("command+space", "win+r", "alt+f2")


def _is_addressbar_key(key: str) -> bool:
    k = (key or "").strip().lower()
    return k in ("ctrl+l", "command+l", "alt+d")


def _ensure_type_then_enter_wait(actions: List[Action], wait_ms: int) -> List[Action]:
    out: List[Action] = []
    i = 0
    while i < len(actions):
        a = actions[i]
        if (
            a.type == "type_text"
            and i + 1 < len(actions)
            and actions[i + 1].type == "key"
            and (actions[i + 1].key or "").strip().lower() == "enter"
        ):
            out.append(a)
            out.append(Action(type="wait", ms=wait_ms))
            out.append(actions[i + 1])
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def _ensure_settle_after_enter(actions: List[Action], min_wait_ms: int) -> List[Action]:
    out = list(actions)
    last_enter_idx = None
    for idx, a in enumerate(out):
        if a.type == "key" and (a.key or "").strip().lower() == "enter":
            last_enter_idx = idx

    if last_enter_idx is None:
        return out

    if last_enter_idx + 1 < len(out) and out[last_enter_idx + 1].type == "wait":
        ms = int(out[last_enter_idx + 1].ms or 0)
        if ms < min_wait_ms:
            out[last_enter_idx + 1].ms = min_wait_ms
        return out

    out.append(Action(type="wait", ms=min_wait_ms))
    return out


def _cap_actions(actions: List[Action], max_n: int) -> List[Action]:
    if len(actions) <= max_n:
        return actions
    if actions[-1].type == "wait":
        core = actions[: max_n - 1]
        return core + [actions[-1]]
    return actions[:max_n]


def _infer_info_kind(instruction: str) -> Optional[str]:
    s = (instruction or "").lower()

    # “Search for …” often means “obtain info”; your examples imply you want extraction.
    # Keep this conservative: only gate tasks where a concrete value is expected.
    if any(k in s for k in ("weather", "temperature", "forecast")):
        return "weather"
    if any(k in s for k in ("flight", "flights", "airfare", "ticket price")):
        return "flights"
    if any(k in s for k in ("price", "cost", "how much", "exchange rate", "rate")):
        return "price"
    if any(k in s for k in ("time in", "what time", "current time")):
        return "time"
    return None


_TEMP_RE = re.compile(r"(-?\d{1,2})\s*°\s*([cCfF])")
_MONEY_RE = re.compile(r"(\b\d[\d\s.,]{1,8}\s*(pln|zl|usd|eur|gbp)\b|[€$£]\s*\d[\d\s.,]{1,8})", re.IGNORECASE)
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")


def _done_summary_satisfies(kind: str, summary: str) -> bool:
    s = (summary or "").strip()
    if not s:
        return False

    sl = s.lower()

    # Reject the common “milestone, not answer” summaries
    if any(p in sl for p in ("search results", "displayed search results", "results are displayed")):
        return False

    if kind == "weather":
        # require a temperature mention
        return bool(_TEMP_RE.search(s))
    if kind == "flights":
        # require some concrete money/price, or at least a strong hint with times
        return bool(_MONEY_RE.search(s)) or bool(_TIME_RE.search(s))
    if kind == "price":
        return bool(_MONEY_RE.search(s)) or any(tok in sl for tok in ("usd", "eur", "pln", "gbp", "€", "$", "£"))
    if kind == "time":
        return bool(_TIME_RE.search(s))
    return True


class KaiVMAgent:
    def __init__(
        self,
        planner: GeminiPlanner,
        kbd: KeyboardHID,
        mouse: MouseHID,
        cfg: AgentConfig,
        abs_mouse: Optional[AbsoluteMouseHID] = None,
    ) -> None:
        self.planner = planner
        self.kbd = kbd
        self.mouse = mouse
        self.abs_mouse = abs_mouse
        self.cfg = cfg

        self._prev_jpeg: Optional[bytes] = None
        self._prev_hash: Optional[str] = None
        self._last_actions_text: str = ""
        self._last_seen_mtime: float = 0.0
        
        self._last_abs_x: Optional[int] = None
        self._last_abs_y: Optional[int] = None

    def _read_latest(self) -> bytes:
        p = self.cfg.latest_jpg
        if not p.exists():
            raise FileNotFoundError(f"latest frame missing: {p} (run `kaivm capture`?)")
        return p.read_bytes()

    def _dump_last_sent(self, jpeg: bytes) -> None:
        if not self.cfg.dump_last_sent:
            return
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            (RUN_DIR / "last_sent.jpg").write_bytes(jpeg)
        except Exception:
            pass

    def _to_abs_hid(self, x: int, y: int) -> tuple[int, int]:
        # Normalize from 0-1000 scale to 0.0-1.0
        nx = x / 1000.0
        ny = y / 1000.0
        
        # Apply calibration
        nx = nx * self.cfg.cal_x_scale + self.cfg.cal_x_offset
        ny = ny * self.cfg.cal_y_scale + self.cfg.cal_y_offset
        
        # Scale to HID
        hx = int(round(nx * 32767))
        hy = int(round(ny * 32767))
        return hx, hy

    def _execute(self, a: Action, screen_w: int = 1920, screen_h: int = 1080) -> Optional[str]:
        if a.type == "wait":
            time.sleep((a.ms or 0) / 1000.0)
            return None

        if self.cfg.dry_run:
            return None

        if a.type == "mouse_move":
            if a.x is not None and a.y is not None and self.abs_mouse:
                x_abs, y_abs = self._to_abs_hid(a.x, a.y)
                
                log.info(f"MoveAbs: ({a.x}, {a.y}) -> ({x_abs}, {y_abs}) [cal s={self.cfg.cal_x_scale:.2f},{self.cfg.cal_y_scale:.2f} o={self.cfg.cal_x_offset:.2f},{self.cfg.cal_y_offset:.2f}]")
                
                self.abs_mouse.move(x_abs, y_abs)
                self._last_abs_x = x_abs
                self._last_abs_y = y_abs
                return None
            
            # Fallback to relative
            self.mouse.move(a.dx or 0, a.dy or 0)
            return None

        if a.type == "mouse_click":
            if self.abs_mouse:
                if a.x is not None and a.y is not None:
                    x_abs, y_abs = self._to_abs_hid(a.x, a.y)
                    
                    log.info(f"ClickAbs: ({a.x}, {a.y}) -> ({x_abs}, {y_abs})")
                    self.abs_mouse.click(x_abs, y_abs, a.button or "left")
                    self._last_abs_x = x_abs
                    self._last_abs_y = y_abs
                    return None

                if self._last_abs_x is not None and self._last_abs_y is not None:
                    # Click at last known position
                    self.abs_mouse.click(self._last_abs_x, self._last_abs_y, a.button or "left")
                    return None
            
            self.mouse.click(a.button or "left")
            return None

        if a.type == "type_text":
            assert a.text is not None
            if not self.cfg.allow_danger and is_dangerous_text(a.text):
                return f"Refused dangerous type_text without --allow-danger: {a.text!r}"
            self.kbd.send_text(a.text)
            return None

        if a.type == "key":
            raw = (a.key or "").strip()
            if not raw:
                return "Empty key action"

            if self.kbd.send_hotkey(raw):
                return None

            up = raw.upper()
            if up in KEY_ALIASES:
                self.kbd.send_text(KEY_ALIASES[up][0])
                return None

            if len(raw) == 1:
                self.kbd.send_text(raw)
                return None

            return f"Unknown key alias/hotkey: {raw}"

        if a.type == "done":
            return a.summary or "done"

        return f"Unsupported action type: {a.type}"

    def run(self, instruction: str) -> str:
        t0 = time.time()

        if self.cfg.do_replug:
            usb_replug()

        # Startup frame check: ensure we have a reasonably fresh frame
        if self.cfg.latest_jpg.exists():
            try:
                st = self.cfg.latest_jpg.stat()
                age = time.time() - st.st_mtime
                if age > 2.0:
                    log.info("Frame is stale (%.1fs old), waiting for fresh one...", age)
                    _wait_for_new_frame(self.cfg.latest_jpg, st.st_mtime, timeout_s=3.0, poll_s=0.1)
                self._last_seen_mtime = self.cfg.latest_jpg.stat().st_mtime
            except Exception:
                self._last_seen_mtime = 0.0

        info_kind = _infer_info_kind(instruction)

        for step_idx in range(1, self.cfg.max_steps + 1):
            if STOP_FILE.exists():
                return "Stopped: /tmp/kaivm.stop present"

            if (time.time() - t0) > self.cfg.overall_timeout_s:
                return f"Timeout after {self.cfg.overall_timeout_s:.1f}s"

            got_new = _wait_for_new_frame(
                self.cfg.latest_jpg,
                self._last_seen_mtime,
                timeout_s=self.cfg.pre_plan_frame_timeout_s,
                poll_s=self.cfg.frame_poll_s,
            )
            note = ""
            if not got_new:
                note = "Frame did not update recently; be cautious and avoid repeating actions."

            jpeg = self._read_latest()
            self._dump_last_sent(jpeg)

            try:
                self._last_seen_mtime = self.cfg.latest_jpg.stat().st_mtime
            except Exception:
                pass

            cur_hash = _hash_bytes(jpeg)
            screen_unchanged = (self._prev_hash is not None and cur_hash == self._prev_hash)
            if screen_unchanged:
                note = (note + " " if note else "") + "Screen appears unchanged vs previous frame."

            today = time.strftime("%Y-%m-%d")

            plan = self.planner.plan(
                instruction=instruction,
                jpeg_bytes=jpeg,
                prev_jpeg_bytes=self._prev_jpeg,
                last_actions_text=self._last_actions_text,
                allow_danger=self.cfg.allow_danger,
                step_idx=step_idx,
                max_steps=self.cfg.max_steps,
                note=(note or ""),
                today=today,
            )

            actions = _normalize_actions(parse_plan(plan))

            # Smart timing fixes:
            actions = _ensure_type_then_enter_wait(actions, wait_ms=self.cfg.type_to_enter_wait_ms)

            if any(a.type == "key" and _is_launcher_key(a.key or "") for a in actions):
                actions = _ensure_settle_after_enter(actions, min_wait_ms=self.cfg.app_launch_settle_ms)

            if any(a.type == "key" and _is_addressbar_key(a.key or "") for a in actions):
                actions = _ensure_settle_after_enter(actions, min_wait_ms=self.cfg.search_submit_settle_ms)

            # Anti-loop: if screen unchanged AND model tries enter spam, replace with ESC once.
            if screen_unchanged:
                only_enterish = all(a.type in ("key", "wait", "done") for a in actions) and any(
                    a.type == "key" and (a.key or "").strip().lower() == "enter" for a in actions
                )
                if only_enterish and "key(enter)" in (self._last_actions_text or ""):
                    actions = [Action(type="key", key="esc"), Action(type="wait", ms=700)]

            # DONE gating: don’t allow “results shown” to count as completion for info tasks.
            done_actions = [a for a in actions if a.type == "done"]
            if done_actions:
                d = done_actions[-1]
                summary = d.summary or ""

                # Enforce minimal horizon to reduce “first-looks-done”
                if step_idx < self.cfg.min_steps_before_done:
                    log.info("Rejecting premature done at step %d (<%d).", step_idx, self.cfg.min_steps_before_done)
                    actions = [a for a in actions if a.type != "done"]
                    if not actions:
                        actions = [Action(type="wait", ms=1200)]
                    # next step will replan with better view

                elif info_kind is not None and not _done_summary_satisfies(info_kind, summary):
                    log.info("Rejecting done: insufficient info for kind=%s summary=%r", info_kind, summary[:200])
                    actions = [a for a in actions if a.type != "done"]

                    # If model wants to stop but didn’t extract, force another observation after a bit.
                    if not actions:
                        # Give the page time to finish rendering so next screenshot contains the data.
                        actions = [Action(type="wait", ms=1800)]

            actions = _cap_actions(actions, self.cfg.max_actions_per_step)

            if self.cfg.confirm and not _confirm_batch(actions):
                return "Stopped by user (confirm)"

            any_input = any(a.type in ("key", "type_text", "mouse_move", "mouse_click") for a in actions)
            
            # Get screen size for absolute mouse
            screen_w, screen_h = get_image_size(jpeg)

            for a in actions:
                res = self._execute(a, screen_w, screen_h)
                if res is not None and a.type == "done":
                    return res
                if res is not None:
                    log.warning("Action result: %s", res)

            if any_input:
                _wait_for_new_frame(
                    self.cfg.latest_jpg,
                    self._last_seen_mtime,
                    timeout_s=self.cfg.post_action_frame_timeout_s,
                    poll_s=self.cfg.frame_poll_s,
                )
                try:
                    self._last_seen_mtime = self.cfg.latest_jpg.stat().st_mtime
                except Exception:
                    pass

            self._prev_jpeg = jpeg
            self._prev_hash = cur_hash
            self._last_actions_text = _actions_brief(actions)

            time.sleep(self.cfg.step_sleep)

        return f"Stopped after max steps ({self.cfg.max_steps})"

