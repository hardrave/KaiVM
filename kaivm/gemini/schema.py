# JSON Schema for Gemini Structured Outputs.
# Keep it reasonably strict, but not "too clever".
# (We still validate defensively in our own code.)

ACTION_TYPES = [
    "wait",
    "mouse_move",
    "mouse_click",
    "type_text",
    "key",
    "done",
]

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of the current state and why these actions were chosen."
        },
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "type": {"type": "string", "enum": ACTION_TYPES},
                    "ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                    "dx": {"type": "integer", "minimum": -127, "maximum": 127},
                    "dy": {"type": "integer", "minimum": -127, "maximum": 127},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "text": {"type": "string", "maxLength": 2000},
                    "key": {"type": "string", "maxLength": 64},
                    "summary": {"type": "string", "maxLength": 2000},
                },
                "required": ["type"],
            },
        }
    },
    "required": ["reasoning", "actions"],
}

