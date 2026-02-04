from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from google import genai
from google.genai import types

from kaivm.gemini.prompts import SYSTEM, USER_TEMPLATE
from kaivm.gemini.schema import PLAN_SCHEMA
from kaivm.util.log import get_logger

log = get_logger("kaivm.gemini")

DEFAULT_MODEL = "gemini-3-flash-preview"


@dataclass
class GeminiPlanner:
    model: str = DEFAULT_MODEL
    thinking_level: str = "low"
    timeout_steps: int = 2
    api_key: Optional[str] = None

    def _client(self) -> genai.Client:
        key = self.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        return genai.Client(api_key=key) if key else genai.Client()

    def plan(
        self,
        instruction: str,
        jpeg_bytes: bytes,
        prev_jpeg_bytes: Optional[bytes] = None,
        last_actions_text: str = "",
        allow_danger: bool = False,
        step_idx: int = 1,
        max_steps: int = 30,
        note: str = "",
        today: str = "",
    ) -> Dict[str, Any]:
        """
        Returns parsed JSON plan dict (validated elsewhere too).
        Includes prior screenshot + last actions to reduce loops and premature done.
        """
        user = USER_TEMPLATE.format(
            instruction=instruction,
            today=today or "(unknown)",
            step_idx=step_idx,
            max_steps=max_steps,
            last_actions=(last_actions_text or "(none)"),
            note=(note or "(none)"),
        )
        if allow_danger:
            user += "\nNote: allow-danger is enabled, but still be careful and incremental.\n"
        else:
            user += "\nNote: allow-danger is NOT enabled. Avoid destructive actions.\n"

        cur_img = types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")
        parts: list[Any] = [
            "CURRENT SCREENSHOT:",
            cur_img,
            user,
        ]

        if prev_jpeg_bytes:
            try:
                prev_img = types.Part.from_bytes(data=prev_jpeg_bytes, mime_type="image/jpeg")
                parts = ["PREVIOUS SCREENSHOT:", prev_img] + parts
            except Exception:
                # If the previous image fails to attach, just omit it.
                pass

        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level),
            response_mime_type="application/json",
            response_json_schema=PLAN_SCHEMA,
        )

        client = self._client()
        last_text: Optional[str] = None

        for attempt in range(self.timeout_steps + 1):
            prompt_parts = list(parts)
            if attempt > 0 and last_text:
                prompt_parts += [
                    "Your previous output was invalid or did not match the schema. "
                    "Return a corrected JSON object ONLY, matching the schema exactly. "
                    f"Previous output:\n{last_text}",
                ]

            resp = client.models.generate_content(
                model=self.model,
                contents=prompt_parts,
                config=cfg,
            )

            txt = (resp.text or "").strip()
            last_text = txt

            try:
                return json.loads(txt)
            except Exception:
                log.warning("Gemini returned non-JSON (attempt %d): %r", attempt + 1, txt[:250])

        raise ValueError("Gemini failed to produce valid JSON after retries")

