"""Skills as procedural memory: Claude writes reusable how-tos as Claude Code skills.

Skills land in ``~/.claude/skills/<name>/SKILL.md`` (override with HERMES_MEMORY_SKILLS_DIR), where
Claude Code loads them in every project. The plugin only ever edits or deletes skills it created
itself (tracked in ``skills.json``); deleted skills are moved to ``skills-archive/``, not destroyed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from . import history, store
from .security import scan

MAX_CHARS = 8000
MAX_DESCRIPTION = 300
REGISTRY = "skills.json"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def skills_dir() -> Path:
    override = os.environ.get("HERMES_MEMORY_SKILLS_DIR")
    if override:
        return Path(override)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config_dir) if config_dir else Path.home() / ".claude") / "skills"


def _skill_file(name: str) -> Path:
    return skills_dir() / name / "SKILL.md"


def _registry(home: Path) -> dict[str, dict]:
    path = home / REGISTRY
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    entries = data.get("owned", {}) if isinstance(data, dict) else {}
    return entries if isinstance(entries, dict) else {}


def owned(home: Path) -> list[str]:
    """Skills this plugin created that still exist (a folder the user removed is no longer ours)."""
    return sorted(n for n in _registry(home) if _skill_file(n).exists())


def _record_owned(home: Path, name: str, keep: bool) -> None:
    """Remember the plugin's skills with a fingerprint of the file it last wrote."""
    entries = {n: e for n, e in _registry(home).items() if n != name and _skill_file(n).exists()}
    if keep:
        entries[name] = {"updated": datetime.now(UTC).isoformat(timespec="seconds"), "sha": _fingerprint(name)}
    store.write_text(home / REGISTRY, json.dumps({"owned": entries}, indent=2))


def _fingerprint(name: str) -> str:
    path = _skill_file(name)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


class SkillError(Exception):
    pass


def _validate(home: Path, op: dict) -> None:
    if not isinstance(op, dict):
        raise SkillError("operation must be an object")
    action, name = op.get("action"), op.get("name")
    if action not in ("create", "patch", "delete"):
        raise SkillError("action must be create, patch or delete")
    if not isinstance(name, str) or not _NAME.match(name) or name in _RESERVED:
        raise SkillError("name must be 3-64 chars of lowercase letters, digits and dashes, e.g. 'monthly-close'")
    for key in ("description", "content", "old_text"):
        if op.get(key) is not None and not isinstance(op[key], str):
            raise SkillError(f"{key} must be a string")
    reasons = scan(f"{op.get('description') or ''}\n{op.get('content') or ''}")
    if reasons:
        raise SkillError(f"blocked: {'; '.join(reasons)}")
    exists = _skill_file(name).parent.exists()
    if action == "create":
        description = (op.get("description") or "").strip()
        if not description or "\n" in description or len(description) > MAX_DESCRIPTION:
            raise SkillError(f"description is required: one line, at most {MAX_DESCRIPTION} chars")
        if not (op.get("content") or "").strip():
            raise SkillError("content (the skill's instructions) is required")
        if len(op["content"]) > MAX_CHARS:
            raise SkillError(f"content is over {MAX_CHARS} chars; keep skills focused")
        if exists:
            raise SkillError(f"a skill named {name!r} already exists; pick another name or patch it")
        return
    if name not in owned(home):
        raise SkillError(f"{name!r} was not created by hermes-memory; it is left untouched")
    if _registry(home)[name].get("sha") != _fingerprint(name):
        raise SkillError(f"{name!r} was edited outside hermes-memory since it was saved; it is left untouched")
    if action == "patch":
        _patched_text(op)


def _split(text: str) -> tuple[str, str]:
    """(frontmatter including its closing ---, body)."""
    match = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    return (match.group(0), text[match.end():]) if match else ("", text)


def _patched_text(op: dict) -> str:
    """Patch the body only; the frontmatter (name, description) is never rewritten by a patch."""
    head, body = _split(_skill_file(op["name"]).read_text(encoding="utf-8"))
    old, new = op.get("old_text") or "", op.get("content") or ""
    if not old or body.count(old) != 1:
        raise SkillError("old_text must match exactly one span of the skill's instructions")
    patched = body.replace(old, new)
    if len(patched) > MAX_CHARS:
        raise SkillError(f"the patched skill would be over {MAX_CHARS} chars")
    return head + patched


def _apply(home: Path, op: dict) -> None:
    name, path = op["name"], _skill_file(op["name"])
    if op["action"] == "create":
        path.parent.mkdir(parents=True, exist_ok=False)
        # json.dumps gives a valid YAML double-quoted string, so ':' or '#' cannot break the frontmatter.
        description = json.dumps(op["description"].strip(), ensure_ascii=False)
        store.write_text(path, f"---\nname: {name}\ndescription: {description}\n---\n\n{op['content'].strip()}\n")
        _record_owned(home, name, True)
    elif op["action"] == "patch":
        store.write_text(path, _patched_text(op))
        _record_owned(home, name, True)
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        archive = home / "skills-archive" / f"{name}-{stamp}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path.parent), str(archive))
        _record_owned(home, name, False)


def describe(op: dict) -> str:
    what = (op.get("description") or op.get("old_text") or "").strip().replace("\n", " ")
    return f"{op.get('action')} skill {op.get('name')}: {what[:100]}"


def submit(home: Path, op: dict) -> dict:
    """Validate a skill change, then apply it (approval off) or stage it for the user's approval."""
    try:
        _validate(home, op)
    except SkillError as exc:
        event = "blocked" if str(exc).startswith("blocked") else "refused"
        history.record(home, event, target="skill", detail=describe(op) if isinstance(op, dict) else "",
                       reason=str(exc))
        return {"success": False, "error": str(exc)}
    with store.locked(home):
        if store.load_config(home)["approval"] == "off":
            _apply(home, op)
            history.record(home, "saved", target="skill", detail=describe(op))
            return {"success": True, "applied": True, "path": str(_skill_file(op["name"])),
                    "message": "skill saved; Claude Code loads it in new sessions"}
        pinned = [_fingerprint(op["name"])] if op["action"] != "create" else []
        return store.stage(home, "skill", [op], pinned, "skills always wait for your approval")


def apply_staged(home: Path, item: dict) -> str | None:
    """Apply an approved staged skill change. Returns an error message, or None on success."""
    op = item["operations"][0]
    if item["pinned"] and _fingerprint(op["name"]) != item["pinned"][0]:
        return "the skill changed since this was staged; reject it"
    try:
        _validate(home, op)
        _apply(home, op)
    except (SkillError, OSError) as exc:
        return str(exc)
    return None


def render(home: Path) -> str:
    names = owned(home)
    if not names:
        return "No skills created by hermes-memory yet."
    return "\n".join(f"{n:<32} {_skill_file(n)}" for n in names)
