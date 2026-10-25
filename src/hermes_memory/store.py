"""The shared notebook: USER.md + MEMORY.md, capped, scanned, and optionally approval-gated.

Layout of the memory home (default ``~/.claude/hermes-memory``, override with HERMES_MEMORY_HOME):

    USER.md       entries about the user, separated by "\\n§\\n"
    MEMORY.md     entries about the user's work, same format
    pending.json  writes waiting for the user's approval
    config.json   {"approval": "off" | "auto" | "on", "user_char_limit": ..., "memory_char_limit": ...}
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .security import scan

DELIM = "\n§\n"
ACTIONS = ("add", "patch", "remove")
MODES = ("off", "auto", "on")
TARGETS = {
    "user": {"file": "USER.md", "limit_key": "user_char_limit",
             "title": "USER PROFILE (who the user is, how they like to work)"},
    "memory": {"file": "MEMORY.md", "limit_key": "memory_char_limit",
               "title": "MEMORY (your notes about the user's work, shared across projects)"},
}
DEFAULT_CONFIG = {"approval": "auto", "user_char_limit": 1375, "memory_char_limit": 2200}

# In "auto" mode a user-profile note still waits for approval when it reads like a work rule.
_RULE_WORDS = re.compile(r"\b(always|never|must|mustn't|don't|do not|only ever)\b", re.IGNORECASE)
_NUMBERS = re.compile(r"(\d|₹|\bRs\.?\s)")


class OperationError(Exception):
    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


# ---------- paths, config, files ----------

def default_home() -> Path:
    override = os.environ.get("HERMES_MEMORY_HOME")
    return Path(override) if override else Path.home() / ".claude" / "hermes-memory"


def _write_text(path: Path, text: str) -> None:
    """Atomic write so a crash never leaves a half-written notebook.

    The temp name is unique per call, and the replace is retried because on Windows another
    process (a hook, antivirus, OneDrive) briefly holding the target makes os.replace fail.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


@contextlib.contextmanager
def _locked(home: Path, timeout: float = 10.0, stale_after: float = 30.0):
    """Cross-process lock so two sessions writing at once cannot lose each other's notes."""
    home.mkdir(parents=True, exist_ok=True)
    lock = home / ".lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            with contextlib.suppress(FileNotFoundError):
                if time.time() - lock.stat().st_mtime > stale_after:
                    lock.unlink()
                    continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"memory store is busy ({lock} held)")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


_CORRUPT = object()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _CORRUPT


def load_config(home: Path) -> dict:
    raw = _read_json(home / "config.json", {})
    if not isinstance(raw, dict):  # unreadable config fails closed: everything needs approval
        return {**DEFAULT_CONFIG, "approval": "on"}
    config = {**DEFAULT_CONFIG, **raw}
    if config["approval"] not in MODES:
        config = {**config, "approval": "on"}
    return config


def _save_config(home: Path, config: dict) -> None:
    _write_text(home / "config.json", json.dumps(config, indent=2))


def set_approval(home: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"approval must be one of {', '.join(MODES)}")
    _save_config(home, {**load_config(home), "approval": mode})


def set_limits(home: Path, *, user: int | None = None, memory: int | None = None) -> None:
    config = load_config(home)
    if user is not None:
        config = {**config, "user_char_limit": user}
    if memory is not None:
        config = {**config, "memory_char_limit": memory}
    _save_config(home, config)


def read_entries(home: Path, target: str) -> list[str]:
    path = home / TARGETS[target]["file"]
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    return [e.strip() for e in raw.split(DELIM) if e.strip()] if raw else []


def _write_entries(home: Path, target: str, entries: list[str]) -> None:
    _write_text(home / TARGETS[target]["file"], DELIM.join(entries))


def used_chars(entries: list[str]) -> int:
    return len(DELIM.join(entries))


def _usage(home: Path, target: str, entries: list[str]) -> str:
    return f"{used_chars(entries):,}/{load_config(home)[TARGETS[target]['limit_key']]:,}"


# ---------- pure operation logic ----------

def _find(entries: list[str], old_text: str) -> int:
    if not old_text:
        raise OperationError("old_text is required for patch and remove")
    if old_text in entries:  # an exact whole-entry match wins over substring matches
        return entries.index(old_text)
    hits = [i for i, e in enumerate(entries) if old_text in e]
    if len(hits) > 1:
        raise OperationError(f"old_text {old_text!r} matches more than one entry; use more text",
                             matching_entries=[entries[i] for i in hits])
    if not hits:
        closest = difflib.get_close_matches(old_text, entries, n=3, cutoff=0.0)
        raise OperationError(f"old_text {old_text!r} matches no entry", closest_entries=closest)
    return hits[0]


def apply_operations(entries: list[str], operations: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Apply operations to a copy of ``entries``.

    Returns (new_entries, notes, pinned) where ``pinned`` is the full text of every entry a
    patch/remove targeted, so a staged write can later prove it still applies to the same entry.
    """
    result, notes, pinned = list(entries), [], []
    for op in operations:
        if not isinstance(op, dict):
            raise OperationError("each operation must be an object with an 'action'")
        for key in ("content", "old_text"):
            if op.get(key) is not None and not isinstance(op[key], str):
                raise OperationError(f"{key} must be a string")
        if DELIM in (op.get("content") or ""):
            raise OperationError("content must not contain the entry separator (a line holding only §)")
        action = op.get("action")
        if action == "replace":
            raise OperationError("'replace' is not supported (it wiped multi-fact entries); "
                                 "use 'patch' with old_text as the span to change")
        if action not in ACTIONS:
            raise OperationError(f"unknown action {action!r}; use one of {', '.join(ACTIONS)}")
        content = (op.get("content") or "").strip()
        if action in ("add", "patch") and not content:
            raise OperationError(f"{action} needs non-empty content")
        if action == "add":
            if content in result:
                notes.append(f"duplicate skipped: {content[:60]!r}")
            else:
                result.append(content)
            continue
        idx = _find(result, op.get("old_text", ""))
        pinned.append(result[idx])
        if action == "remove":
            result = result[:idx] + result[idx + 1:]
            continue
        old_text, entry = op["old_text"], result[idx]
        if entry == old_text:
            new_entry = content
        elif entry.count(old_text) > 1:
            raise OperationError(f"old_text {old_text!r} occurs more than once in the entry; use more text")
        else:
            new_entry = entry.replace(old_text, content)
        result = result[:idx] + [new_entry] + result[idx + 1:]
    return result, notes, pinned


def _check_cap(home: Path, target: str, before: list[str], after: list[str]) -> None:
    limit = load_config(home)[TARGETS[target]["limit_key"]]
    size = used_chars(after)
    if size > limit and size > used_chars(before):  # a shrinking batch is always allowed
        raise OperationError(
            f"{TARGETS[target]['file']} at {used_chars(before):,}/{limit:,} chars; this change would "
            f"exceed the limit by {size - limit:,} chars. Retry as ONE operations batch that removes or "
            f"shortens (patch) stale entries from current_entries AND adds this entry.",
            current_entries=before, usage=f"{used_chars(before):,}/{limit:,}")


def _approval_reason(mode: str, target: str, operations: list[dict]) -> str | None:
    if mode == "off":
        return None
    if mode == "on":
        return "approval mode is on"
    if target == "memory":
        return "work rules wait for your approval"
    text = " ".join(op.get("content") or "" for op in operations).replace("\u2019", "'")
    if _RULE_WORDS.search(text):
        return "reads like a rule (always/never/must)"
    if _NUMBERS.search(text):
        return "contains a number or amount"
    if any(op.get("action") == "remove" for op in operations):
        return "removes an existing note"
    return None


# ---------- public API ----------

def submit(home: Path, target: str, operations: list[dict]) -> dict:
    """Validate a batch of operations, then write it or stage it for approval."""
    if target not in TARGETS:
        return {"success": False, "error": f"target must be 'user' or 'memory', got {target!r}"}
    if not operations:
        return {"success": False, "error": "operations must be a non-empty list"}
    if not isinstance(operations, list):
        return {"success": False, "error": "operations must be a list"}
    for op in operations:
        content = op.get("content") if isinstance(op, dict) else None
        reasons = scan(content) if isinstance(content, str) else []
        if reasons:
            return {"success": False, "error": f"blocked: {'; '.join(reasons)}"}
    with _locked(home):
        before = read_entries(home, target)
        try:
            after, notes, pinned = apply_operations(before, operations)
            _check_cap(home, target, before, after)
        except OperationError as exc:
            return {"success": False, "error": str(exc), **exc.details}

        reason = _approval_reason(load_config(home)["approval"], target, operations)
        if reason is None:
            _write_entries(home, target, after)
            return {"success": True, "applied": True, "usage": _usage(home, target, after),
                    "message": "; ".join(notes) or "saved"}
        staged_id = _stage(home, target, operations, pinned, reason)
    return {"success": True, "applied": False, "staged_id": staged_id, "usage": _usage(home, target, before),
            "message": f"waiting for approval as #{staged_id} ({reason}). Ask the user to confirm; "
                       f"they can run /memory-approve {staged_id} or /memory-reject {staged_id}."}


def _load_pending(home: Path) -> dict:
    path = home / "pending.json"
    data = _read_json(path, {"next_id": 1, "items": []})
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data
    # Keep the unreadable file for inspection instead of silently overwriting it.
    if path.exists():
        os.replace(path, path.with_name(f"pending.corrupt.{int(time.time())}.json"))
    return {"next_id": 1, "items": []}


def _save_pending(home: Path, pending: dict) -> None:
    _write_text(home / "pending.json", json.dumps(pending, indent=2, ensure_ascii=False))


def _stage(home: Path, target: str, operations: list[dict], pinned: list[str], reason: str) -> int:
    pending = _load_pending(home)
    item = {"id": pending["next_id"], "target": target, "operations": operations, "pinned": pinned,
            "reason": reason, "created": datetime.now(UTC).isoformat(timespec="seconds")}
    _save_pending(home, {"next_id": item["id"] + 1, "items": pending["items"] + [item]})
    return item["id"]


def list_pending(home: Path) -> list[dict]:
    return _load_pending(home)["items"]


def _select(home: Path, which: str) -> tuple[dict, list[dict]]:
    pending = _load_pending(home)
    if which == "all":
        return pending, pending["items"]
    return pending, [i for i in pending["items"] if str(i["id"]) == str(which).lstrip("#")]


def approve(home: Path, which: str) -> dict:
    with _locked(home):
        return _approve(home, which)


def _approve(home: Path, which: str) -> dict:
    pending, chosen = _select(home, which)
    if not chosen:
        return {"success": False, "error": f"no waiting memory {which!r}"}
    done, failures = [], []
    for item in chosen:
        current = read_entries(home, item["target"])
        changed = [p for p in item["pinned"] if p not in current]
        if changed:
            failures.append(f"#{item['id']}: the note it targets changed since it was staged; reject it")
            continue
        try:
            after, _, _ = apply_operations(current, item["operations"])
            _check_cap(home, item["target"], current, after)
        except OperationError as exc:
            failures.append(f"#{item['id']}: {exc}")
            continue
        _write_entries(home, item["target"], after)
        done.append(item["id"])
    _save_pending(home, {**pending, "items": [i for i in pending["items"] if i["id"] not in done]})
    if failures:
        return {"success": False, "approved": done, "error": " | ".join(failures)}
    return {"success": True, "approved": done}


def reject(home: Path, which: str) -> dict:
    with _locked(home):
        return _reject(home, which)


def _reject(home: Path, which: str) -> dict:
    pending, chosen = _select(home, which)
    if not chosen:
        return {"success": False, "error": f"no waiting memory {which!r}"}
    ids = {i["id"] for i in chosen}
    _save_pending(home, {**pending, "items": [i for i in pending["items"] if i["id"] not in ids]})
    return {"success": True, "rejected": sorted(ids)}


# ---------- rendering ----------

_GUIDANCE = """\
You have a persistent memory shared across ALL projects (the hermes-memory plugin).
- Save durable facts with the `memory` tool: target "user" for who the user is and how they like to work,
  target "memory" for lessons about their work that apply beyond this chat. Actions: add, patch, remove.
- Save proactively when you learn a preference, a correction, an environment quirk, or a decision.
  Skip trivia, things easy to re-discover, raw data dumps, and session-only details.
- Keep entries short and dense. When a store is above 80%, merge entries before adding new ones.
- This block is a snapshot from session start; writes made now appear in the next session.
- If a write comes back "waiting for approval", tell the user in one line what you want to save and ask
  them to confirm. Never claim it is saved."""


def render_block(home: Path, target: str) -> str:
    entries = read_entries(home, target)
    limit = load_config(home)[TARGETS[target]["limit_key"]]
    pct = round(100 * used_chars(entries) / limit) if limit else 0
    bar = "═" * 46
    body = DELIM.join(entries) if entries else "(empty)"
    return f"{bar}\n{TARGETS[target]['title']} [{pct}% — {used_chars(entries):,}/{limit:,} chars]\n{bar}\n{body}"


def render_snapshot(home: Path) -> str:
    parts = [_GUIDANCE, render_block(home, "user"), render_block(home, "memory")]
    waiting = len(list_pending(home))
    if waiting:
        noun = "memory" if waiting == 1 else "memories"
        parts.append(f"{waiting} {noun} waiting for the user's approval — mention /memory-pending once, briefly.")
    return "\n\n".join(parts)


_UNPRINTABLE = re.compile("[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def _one_line(text: Any) -> str:
    """Show model-written text on one line so it cannot forge extra list items or repaint the terminal."""
    return _UNPRINTABLE.sub(lambda m: " ⏎ " if m.group() == "\n" else "\ufffd", str(text))


def render_pending(home: Path) -> str:
    items = list_pending(home)
    if not items:
        return "No memories waiting for approval."
    lines = []
    for item in items:
        ops = "; ".join(f"{op['action']}: {_one_line(op.get('content') or op.get('old_text'))}"
                        for op in item["operations"])
        lines.append(f"#{item['id']}  [{item['target'].upper()}]  {ops}\n"
                     f"    reason: {item['reason']} · {item['created']}")
        lines.extend(f"    replaces/removes: {_one_line(p)}" for p in item["pinned"])
    return "\n".join(lines)
