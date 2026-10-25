"""Append-only log of every memory event, so the user can see what was captured, when and why."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

LOG_FILE = "history.jsonl"


def _project() -> str:
    path = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(path).name or path


def record(home: Path, event: str, *, target: str | None = None, detail: str = "",
           reason: str | None = None) -> None:
    """Append one event. Logging must never break a memory write, so IO errors are swallowed."""
    entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, "target": target,
             "project": _project(), "detail": detail, "reason": reason}
    try:
        home.mkdir(parents=True, exist_ok=True)
        with (home / LOG_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent(home: Path, limit: int = 20) -> list[dict]:
    path = home / LOG_FILE
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events[-limit:]


_ICONS = {"saved": "💾", "staged": "⏳", "approved": "✅", "rejected": "🗑", "blocked": "⛔",
          "refused": "⚠", "approve-failed": "⚠"}


def render(home: Path, limit: int = 20) -> str:
    events = recent(home, limit)
    if not events:
        return "No memory activity yet."
    lines = []
    for e in events:
        target = f"[{(e.get('target') or '').upper()}]" if e.get("target") else ""
        why = f"  ({e['reason']})" if e.get("reason") else ""
        event = str(e.get("event", "?"))
        lines.append(f"{e.get('ts', '?')}  {_ICONS.get(event, '·')} {event:<14} "
                     f"{target:<9} {e.get('project', '')}: {e.get('detail', '')}{why}")
    return "\n".join(lines)
