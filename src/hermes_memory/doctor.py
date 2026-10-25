"""`/memory-doctor`: a health check the user can run when memory seems not to work."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from . import history, store

Check = tuple[str, bool, str]  # (name, ok, detail)


def _home_writable(home: Path) -> Check:
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return ("memory folder writable", True, str(home))
    except OSError as exc:
        return ("memory folder writable", False, f"{home}: {exc}")


def _config(home: Path) -> Check:
    path = home / "config.json"
    if not path.exists():
        return ("config", True, "defaults (approval auto)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ("config", False, f"{path} is unreadable; approval falls back to ON. Fix or delete the file.")
    mode = raw.get("approval", "auto") if isinstance(raw, dict) else None
    if mode not in store.MODES:
        return ("config", False, f"approval={mode!r} is not off/auto/on; treated as ON")
    return ("config", True, f"approval {mode}")


def _stores(home: Path) -> list[Check]:
    config = store.load_config(home)
    checks = []
    for target, meta in store.TARGETS.items():
        entries = store.read_entries(home, target)
        used, limit = store.used_chars(entries), config[meta["limit_key"]]
        ok = used <= limit
        detail = f"{len(entries)} notes, {used:,}/{limit:,} chars"
        checks.append((meta["file"], ok, detail if ok else f"{detail} — over the limit; remove or merge notes"))
    return checks


def _pending(home: Path) -> Check:
    path = home / "pending.json"
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ("approval queue", False, f"{path} is unreadable")
    waiting = len(store.list_pending(home)) if path.exists() else 0
    return ("approval queue", True, f"{waiting} waiting")


def _lock(home: Path) -> Check:
    lock = home / ".lock"
    if not lock.exists():
        return ("write lock", True, "free")
    age = time.time() - lock.stat().st_mtime
    if age > 30:
        return ("write lock", False, f"stale lock {age:.0f}s old at {lock}; it is cleared on the next write")
    return ("write lock", True, "held by a write in progress")


def _mcp_sdk() -> Check:
    ok = importlib.util.find_spec("mcp") is not None
    return ("MCP SDK installed", ok, "ok" if ok else "run `uv sync` in the plugin folder")


def _activity(home: Path) -> Check:
    last = history.recent(home, limit=1)
    if not last:
        return ("activity", True, "no saves yet — in a new chat, ask Claude to remember something")
    return ("activity", True, f"last: {last[0].get('event')} at {last[0].get('ts')}")


def run(home: Path) -> tuple[bool, str]:
    checks = [_home_writable(home), _config(home), *_stores(home), _pending(home), _lock(home),
              _mcp_sdk(), _activity(home)]
    lines = [f"{'OK  ' if ok else 'FAIL'}  {name:<24} {detail}" for name, ok, detail in checks]
    lines.append("")
    lines.append("If all checks pass but Claude never uses the memory tool: run /mcp and confirm "
                 "'plugin:hermes-memory' is connected, then start a new chat.")
    return all(ok for _, ok, _ in checks), "\n".join(lines)
