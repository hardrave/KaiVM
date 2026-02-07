from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from google import genai
from google.genai import types
from io import BytesIO

from kaivm.gemini.prompts import SYSTEM, USER_TEMPLATE
from kaivm.gemini.schema import PLAN_SCHEMA
from kaivm.util.image import get_image_size, process_image
from kaivm.util.log import get_logger

log = get_logger("kaivm.gemini")

DEFAULT_MODEL = "gemini-3-flash-preview"


@dataclass
class GeminiPlanner:
    model: str = DEFAULT_MODEL
    thinking_level: Optional[str] = None
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
        # Get original image stats
        orig_w, orig_h = get_image_size(jpeg_bytes)
        
        # Process: Resize + Grid
        # We use max_dim=2048 to speed up Gemini processing and upload
        jpeg_bytes_processed, proc_w, proc_h = process_image(jpeg_bytes, max_dim=2048)
        
        user = USER_TEMPLATE.format(
            instruction=instruction,
            today=today or "(unknown)",
            step_idx=step_idx,
            max_steps=max_steps,
            last_actions=(last_actions_text or "(none)"),
            note=(note or "(none)"),
        )
        
        # Inject resolution info (Model sees REAL resolution via grid labels)
        user += f"Screen Resolution: Normalized 0-1000 scale.\n"
        user += "A 10x10 RED GRID with coordinate labels is overlaid on the screenshot.\n"
        user += f"The coordinates are NORMALIZED from 0 to 1000 for both X and Y axes.\n"
        user += f"Top-Left is (0, 0). Bottom-Right is (1000, 1000).\n"
        user += f"Use the grid labels to determine the position of elements.\n"

        if allow_danger:
            user += "\nNote: allow-danger is enabled, but still be careful and incremental.\n"
        else:
            user += "\nNote: allow-danger is NOT enabled. Avoid destructive actions.\n"

        cur_img = types.Part.from_bytes(data=jpeg_bytes_processed, mime_type="image/jpeg")
        parts: list[Any] = [
            "CURRENT SCREENSHOT:",
            cur_img,
            user,
        ]

        if prev_jpeg_bytes:
            try:
                # Also resize previous image for consistency if we wanted, 
                # but for now just sending it raw or we could process it too.
                # Processing it is safer for context matching.
                prev_proc, _, _ = process_image(prev_jpeg_bytes, max_dim=2048)
                prev_img = types.Part.from_bytes(data=prev_proc, mime_type="image/jpeg")
                parts = ["PREVIOUS SCREENSHOT:", prev_img] + parts
            except Exception:
                pass

        # Configure thinking only if requested
        thinking_cfg = None
        if self.thinking_level:
            thinking_cfg = types.ThinkingConfig(thinking_level=self.thinking_level)

        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            thinking_config=thinking_cfg,
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
                plan = json.loads(txt)
                return plan
            except Exception:
                log.warning("Gemini returned non-JSON (attempt %d): %r", attempt + 1, txt[:250])

        raise ValueError("Gemini failed to produce valid JSON after retries")

    def ask(
        self,
        instruction: str,
        jpeg_bytes: bytes,
    ) -> str:
        """
        Simple Q&A about the screen.
        """
        # We might want to resize just to be safe/fast
        jpeg_bytes_processed, _, _ = process_image(jpeg_bytes, max_dim=2048)
        
        parts = [
            types.Part.from_bytes(data=jpeg_bytes_processed, mime_type="image/jpeg"),
            instruction
        ]
        
        client = self._client()
        
        resp = client.models.generate_content(
            model=self.model,
            contents=parts,
        )
        return resp.text or ""

    def check_condition(
        self,
        condition: str,
        jpeg_bytes: bytes,
    ) -> Dict[str, Any]:
        """
        Checks if a condition is met on the screen.
        Returns {"met": bool, "reasoning": str}
        """
        jpeg_bytes_processed, _, _ = process_image(jpeg_bytes, max_dim=2048)
        
        prompt = f"""
        Analyze the screenshot and determine if the following condition is met:
        
        CONDITION: {condition}
        
        Respond with a JSON object containing:
        - "met": boolean (true if condition is met, false otherwise)
        - "reasoning": string (explanation of your decision)
        """
        
        parts = [
            types.Part.from_bytes(data=jpeg_bytes_processed, mime_type="image/jpeg"),
            prompt
        ]
        
        schema = {
            "type": "OBJECT",
            "properties": {
                "met": {"type": "BOOLEAN"},
                "reasoning": {"type": "STRING"},
            },
            "required": ["met", "reasoning"]
        }
        
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        )
        
        client = self._client()
        
        try:
            resp = client.models.generate_content(
                model=self.model,
                contents=parts,
                config=cfg,
            )
            return json.loads(resp.text)
        except Exception as e:
            log.error(f"Condition check failed: {e}")
            return {"met": False, "reasoning": f"Error: {e}"}

