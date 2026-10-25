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

from . import history
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
DEFAULT_CONFIG = {"approval": "auto", "user_char_limit": 1375, "memory_char_limit": 2200, "learn_every": 6}

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


def set_learn_every(home: Path, every: int) -> None:
    """How many user prompts between learning nudges (0 turns them off)."""
    _save_config(home, {**load_config(home), "learn_every": max(0, int(every))})


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
            # The blocked text itself is not logged: it may be the very secret we refused.
            history.record(home, "blocked", target=target, detail="(content not logged)",
                           reason="; ".join(reasons))
            return {"success": False, "error": f"blocked: {'; '.join(reasons)}"}
    with _locked(home):
        before = read_entries(home, target)
        try:
            after, notes, pinned = apply_operations(before, operations)
            _check_cap(home, target, before, after)
        except OperationError as exc:
            history.record(home, "refused", target=target, detail=_describe(operations), reason=str(exc))
            return {"success": False, "error": str(exc), **exc.details}

        reason = _approval_reason(load_config(home)["approval"], target, operations)
        if reason is None:
            _write_entries(home, target, after)
            history.record(home, "saved", target=target, detail=_describe(operations))
            return {"success": True, "applied": True, "usage": _usage(home, target, after),
                    "message": "; ".join(notes) or "saved"}
        return {**stage(home, target, operations, pinned, reason), "usage": _usage(home, target, before)}


def stage(home: Path, target: str, operations: list[dict], pinned: list[str], reason: str) -> dict:
    """Queue a write for approval (caller holds the lock).

    Notes and rules may be confirmed in chat, one at a time. Skills never are: their full text has to be
    read with /memory-pending and approved with /memory-approve.
    """
    in_chat = target != "skill"
    staged_id, ask = _stage(home, target, operations, pinned, reason, in_chat)
    history.record(home, "staged", target=target, detail=f"#{staged_id} {_describe(operations)}", reason=reason)
    result = {"success": True, "applied": False, "staged_id": staged_id, "ask_user": ask}
    if not in_chat:
        return {**result, "message": f"Skill waiting for approval as #{staged_id}. Tell the user in one line to read "
                                     f"it with /memory-pending and approve it with /memory-approve {staged_id}. "
                                     f"Do not ask for a yes in chat: a chat reply cannot approve a skill."}
    if ask:
        question = question_for(staged_id, target, operations)
        return {**result, "question": question,
                "message": f"Waiting for approval as #{staged_id} ({reason}). End your reply with exactly this "
                           f"question as its own last line, with nothing after it: {question}"}
    return {**result, "message": f"Waiting for approval as #{staged_id} ({reason}). Another note is already being "
                                 f"asked about, so do not ask now; the user can review it with /memory-pending."}


def question_for(staged_id: int, target: str, operations: list[dict]) -> str:
    kind = "a note about you" if target == "user" else "a rule"
    return (f"Save as {kind}? #{staged_id} — \"{_describe(operations)[:200]}\" — "
            f"reply yes, no, or tell me what to change.")


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


def _stage(home: Path, target: str, operations: list[dict], pinned: list[str], reason: str,
           in_chat: bool) -> tuple[int, bool]:
    pending = _load_pending(home)
    ask = in_chat and not any(i.get("ask") for i in pending["items"])
    item = {"id": pending["next_id"], "target": target, "operations": operations, "pinned": pinned,
            "reason": reason, "ask": ask, "armed": None, "created": datetime.now(UTC).isoformat(timespec="seconds")}
    _save_pending(home, {"next_id": item["id"] + 1, "items": pending["items"] + [item]})
    return item["id"], ask


# ---------- in-chat confirmation state ----------
# "ask"   = Claude was told to put this item's question to the user.
# "armed" = the Stop hook saw that question at the end of Claude's reply in session S at time T.
# Only an armed item, answered in the same session within ARM_TTL, can be approved by a chat reply.

ARM_TTL_SECONDS = 30 * 60


def _update_items(home: Path, change) -> None:
    with _locked(home):
        pending = _load_pending(home)
        _save_pending(home, {**pending, "items": [change(i) for i in pending["items"]]})


def arm(home: Path, session_id: str, last_reply: str) -> int | None:
    """Called when Claude finishes a reply: arm the asked item if its question ends that reply."""
    tail = last_reply.strip()[-400:]
    target = next((i for i in list_pending(home)
                   if i.get("ask") and f"#{i['id']}" in tail and "reply yes" in tail.lower()), None)
    if target is None:
        return None
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _update_items(home, lambda i: {**i, "armed": {"session": session_id, "at": now}} if i["id"] == target["id"] else i)
    return target["id"]


def armed_item(home: Path, session_id: str) -> dict | None:
    for item in list_pending(home):
        armed = item.get("armed")
        if not isinstance(armed, dict) or armed.get("session") != session_id:
            continue
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(armed["at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= age <= ARM_TTL_SECONDS:
            return item
    return None


def disarm(home: Path, staged_id: int) -> None:
    """The user moved on without answering: stop asking; the item stays in /memory-pending."""
    _update_items(home, lambda i: {**i, "ask": False, "armed": None} if i["id"] == staged_id else i)


def promote_next(home: Path) -> dict | None:
    """After a decision, choose the oldest waiting note (not skill) as the next one to ask about."""
    with _locked(home):
        pending = _load_pending(home)
        if any(i.get("ask") for i in pending["items"]):
            return None
        nxt = next((i for i in pending["items"] if i["target"] != "skill"), None)
        if nxt is None:
            return None
        items = [{**i, "ask": True} if i["id"] == nxt["id"] else i for i in pending["items"]]
        _save_pending(home, {**pending, "items": items})
        return {**nxt, "ask": True}


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
        detail = f"#{item['id']} {_describe(item['operations'])}"
        if item["target"] == "skill":
            from . import skills  # skills imports store; resolve the cycle at call time

            error = skills.apply_staged(home, item)
            if error:
                failures.append(f"#{item['id']}: {error}")
                history.record(home, "approve-failed", target="skill", detail=detail, reason=error)
            else:
                history.record(home, "approved", target="skill", detail=detail)
                done.append(item["id"])
            continue
        current = read_entries(home, item["target"])
        changed = [p for p in item["pinned"] if p not in current]
        error = None
        if changed:
            error = "the note it targets changed since it was staged; reject it"
        else:
            try:
                after, _, _ = apply_operations(current, item["operations"])
                _check_cap(home, item["target"], current, after)
            except OperationError as exc:
                error = str(exc)
        if error:
            failures.append(f"#{item['id']}: {error}")
            history.record(home, "approve-failed", target=item["target"], detail=detail, reason=error)
            continue
        _write_entries(home, item["target"], after)
        history.record(home, "approved", target=item["target"], detail=detail)
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
    for item in chosen:
        history.record(home, "rejected", target=item["target"],
                       detail=f"#{item['id']} {_describe(item['operations'])}")
    return {"success": True, "rejected": sorted(ids)}


def _describe(operations: list) -> str:
    parts = []
    for op in operations:
        if isinstance(op, dict) and op.get("name"):  # a skill operation
            what = (op.get("description") or op.get("old_text") or "").strip()
            parts.append(f"{op.get('action')} skill {op['name']}: {_one_line(what)[:100]}")
        elif isinstance(op, dict):
            parts.append(f"{op.get('action')}: {_one_line(op.get('content') or op.get('old_text') or '')[:120]}")
    return "; ".join(parts)


# ---------- rendering ----------

_GUIDANCE = """\
You have a persistent memory shared across ALL projects (the hermes-memory plugin).
- Save durable facts with the `memory` tool: target "user" for who the user is and how they like to work,
  target "memory" for lessons about their work that apply beyond this chat. Actions: add, patch, remove.
- Save proactively when you learn a preference, a correction, an environment quirk, or a decision.
  Skip trivia, things easy to re-discover, raw data dumps, and session-only details.
- Keep entries short and dense. When a store is above 80%, merge entries before adding new ones.
- This block is a snapshot from session start; writes made now appear in the next session.
- Use the `recall` tool to search past conversations (all projects) when the user refers to earlier
  work or when an earlier decision would stop you re-asking. Do not store chat history in memory.
- When you work out a reusable multi-step procedure (how this user deploys, closes the month, builds a
  report), save it with the `skill` tool instead of a memory note; skills load only when relevant.
- If a write comes back "waiting for approval" with a question, end your reply with that exact question
  as its own last line and nothing after it. The user's next message answers it. Never claim it is saved."""


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
        asked = "  (asked in chat)" if item.get("ask") else ""
        lines.append(f"#{item['id']}  [{item['target'].upper()}]  {_describe(item['operations'])}{asked}\n"
                     f"    reason: {item['reason']} · {item['created']}")
        if item["target"] == "skill":  # show what would actually be written, not just its label
            body = item["operations"][0].get("content") or ""
            if body:
                lines.append(f"    content ({len(body)} chars): {_one_line(body)[:600]}")
        else:
            lines.extend(f"    replaces/removes: {_one_line(p)}" for p in item["pinned"])
    return "\n".join(lines)


# Public aliases for sibling modules (skills, learning).
write_text = _write_text
locked = _locked
