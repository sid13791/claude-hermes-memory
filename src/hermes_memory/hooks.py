"""Claude Code hook handlers. Each reads the hook's JSON from stdin and returns what to print."""

from __future__ import annotations

import json
from typing import Any


def _find_result(value: Any) -> dict | None:
    """Locate the memory tool's result dict inside a tool_response of any shape."""
    if isinstance(value, dict):
        if "success" in value:
            return value
        for child in value.values():
            found = _find_result(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_result(child)
            if found:
                return found
    elif isinstance(value, str):
        try:
            return _find_result(json.loads(value))
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def notice_for(result: dict) -> str:
    if not result.get("success"):
        return f"⚠ Memory not saved: {str(result.get('error', 'unknown error'))[:160]}"
    if result.get("applied"):
        return f"💾 Memory saved ({result.get('usage', '')})"
    return f"⏳ Memory waiting for approval #{result.get('staged_id')} — /memory-pending to review"


def post_tool(stdin_text: str) -> str:
    """PostToolUse on the memory tool: surface a one-line notice to the user."""
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, ValueError):
        return ""
    result = _find_result(payload.get("tool_response")) if isinstance(payload, dict) else None
    if not result:
        return ""
    return json.dumps({"systemMessage": notice_for(result)}, ensure_ascii=False)


def transcript_path(stdin_text: str) -> str | None:
    """Stop / SessionEnd hooks receive the current transcript path in their JSON input."""
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, ValueError):
        return None
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    return path if isinstance(path, str) and path else None
