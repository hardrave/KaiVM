from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

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
    thinking_level: str = "low"  # Gemini 3 supports thinking_level; Flash also supports "minimal". :contentReference[oaicite:5]{index=5}
    timeout_steps: int = 2

    def _client(self) -> genai.Client:
        # SDK will read GEMINI_API_KEY or GOOGLE_API_KEY if not provided. :contentReference[oaicite:6]{index=6}
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        return genai.Client(api_key=api_key) if api_key else genai.Client()

    def plan(self, instruction: str, jpeg_bytes: bytes, allow_danger: bool) -> Dict[str, Any]:
        """
        Returns parsed JSON plan dict (still validated again elsewhere).
        Retries if the model returns invalid JSON.
        """
        user = USER_TEMPLATE.format(instruction=instruction)
        if allow_danger:
            user += "\nNote: allow-danger is enabled, but still be careful and incremental.\n"
        else:
            user += "\nNote: allow-danger is NOT enabled. Avoid destructive actions.\n"

        image_part = types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")

        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level),
            response_mime_type="application/json",
            response_json_schema=PLAN_SCHEMA,
        )

        client = self._client()
        last_text: Optional[str] = None

        for attempt in range(self.timeout_steps + 1):
            prompt_parts = [image_part, user]
            if attempt > 0 and last_text:
                prompt_parts = [
                    image_part,
                    user,
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
                log.warning("Gemini returned non-JSON (attempt %d): %r", attempt + 1, txt[:200])

        raise ValueError("Gemini failed to produce valid JSON after retries")

