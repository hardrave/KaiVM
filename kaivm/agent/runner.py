from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

from kaivm.agent.validate import Action, is_dangerous_text, parse_plan
from kaivm.gemini.client import GeminiPlanner
from kaivm.hid.keyboard import KeyboardHID
from kaivm.hid.mouse import MouseHID
from kaivm.hid.udc import usb_replug
from kaivm.util.log import get_logger
from kaivm.util.paths import LATEST_JPG, STOP_FILE, RUN_DIR

log = get_logger("kaivm.agent")


@dataclass
class AgentConfig:
    latest_jpg: Path = LATEST_JPG
    max_steps: int = 30
    step_sleep: float = 0.5
    overall_timeout_s: float = 120.0
    dry_run: bool = False
    confirm: bool = False
    allow_danger: bool = False
    do_replug: bool = True
    dump_last_sent: bool = True  # NEW: write /run/kaivm/last_sent.jpg each step


KEY_ALIASES = {
    "ENTER": ("\n",),
    "TAB": ("\t",),
    "SPACE": (" ",),
}


def _confirm_batch(actions) -> bool:
    print("\nPlanned actions:")
    for a in actions:
        print(" -", a)
    ans = input("Execute? [y/N] ").strip().lower()
    return ans in ("y", "yes")


def _normalize_actions(actions: List[Action]) -> List[Action]:
    """
    Fix common LLM patterns:
      key: "command" then key: " "  -> key: "command+space"
      key: "command" then key: "space" -> key: "command+space"
    """
    out: List[Action] = []
    i = 0
    while i < len(actions):
        a = actions[i]
        if (
            a.type == "key"
            and (a.key or "").strip().lower() in ("command", "cmd", "gui", "win", "super", "meta")
            and i + 1 < len(actions)
            and actions[i + 1].type == "key"
        ):
            b = actions[i + 1]
            bkey = (b.key or "")
            if bkey == " " or bkey.strip().lower() in ("space",):
                out.append(Action(type="key", key=f"{a.key}+space"))
                i += 2
                continue

        out.append(a)
        i += 1
    return out


class KaiVMAgent:
    def __init__(
        self,
        planner: GeminiPlanner,
        kbd: KeyboardHID,
        mouse: MouseHID,
        cfg: AgentConfig,
    ) -> None:
        self.planner = planner
        self.kbd = kbd
        self.mouse = mouse
        self.cfg = cfg

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

    def _execute(self, a: Action) -> Optional[str]:
        if a.type == "wait":
            time.sleep((a.ms or 0) / 1000.0)
            return None

        if self.cfg.dry_run:
            return None

        if a.type == "mouse_move":
            self.mouse.move(a.dx or 0, a.dy or 0)
            return None

        if a.type == "mouse_click":
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

            # 1) try hotkey combos like command+space, ctrl+l, alt+f4
            if self.kbd.send_hotkey(raw):
                return None

            # 2) fall back to simple aliases (ENTER/TAB/SPACE) -> typing control chars
            up = raw.upper()
            if up in KEY_ALIASES:
                self.kbd.send_text(KEY_ALIASES[up][0])
                return None

            # 3) single char literal
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

        for _step in range(1, self.cfg.max_steps + 1):
            if STOP_FILE.exists():
                return "Stopped: /tmp/kaivm.stop present"

            if (time.time() - t0) > self.cfg.overall_timeout_s:
                return f"Timeout after {self.cfg.overall_timeout_s:.1f}s"

            jpeg = self._read_latest()
            self._dump_last_sent(jpeg)

            plan = self.planner.plan(instruction, jpeg, allow_danger=self.cfg.allow_danger)
            actions = parse_plan(plan)
            actions = _normalize_actions(actions)

            # keep batches small
            actions = actions[:3]

            if self.cfg.confirm and not _confirm_batch(actions):
                return "Stopped by user (confirm)"

            for a in actions:
                res = self._execute(a)
                if res is not None and a.type == "done":
                    return res
                if res is not None:
                    log.warning("Action result: %s", res)

            time.sleep(self.cfg.step_sleep)

        return f"Stopped after max steps ({self.cfg.max_steps})"

