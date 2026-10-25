"""Google Open Knowledge Format (OKF v0.2) bridge: look up company knowledge, draft pages, export memory.

An OKF bundle is a folder of markdown "concepts" with YAML frontmatter (`type` is the only required key).
The plugin reads the bundles the user configured, ranks pages by trust (`verified`), skips deprecated
pages, flags drafts and stale pages, and can write new pages back only as unverified drafts, after the
user approves them with /memory-approve. Spec: github.com/GoogleCloudPlatform/open-knowledge-format
"""

from __future__ import annotations

import importlib.metadata
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from . import history, store
from .security import redact, scan

OKF_VERSION = "0.2"
RESERVED = {"index", "log"}
MAX_FILES = 5000
MAX_PAGE_BYTES = 256 * 1024
EXPORT_MARKER = ".hermes-memory-export"
_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WIN_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_SCALARS = (str, int, float, bool, date)  # date covers datetime
_isjunction = getattr(os.path, "isjunction", lambda _path: False)  # Python 3.12+
_CACHE: dict[str, tuple[tuple[int, int], tuple[dict | None, str]]] = {}
_FRONT = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+\.md)\)")


class OKFError(Exception):
    pass


def _actor() -> str:
    try:
        return f"hermes-memory/{importlib.metadata.version('hermes-memory')}"
    except importlib.metadata.PackageNotFoundError:
        return "hermes-memory/dev"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------- configured bundles ----------

def bundles(home: Path) -> list[Path]:
    return [Path(p) for p in store.load_config(home).get("okf_bundles", [])]


def _absolute(path: str | Path) -> Path:
    """Slash commands run inside the plugin folder, so a relative path would land there: require a full path."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise OKFError(f"give the full folder path (for example C:\\Users\\you\\kb), not {str(path)!r}")
    return p


def add_bundle(home: Path, path: str) -> Path:
    root = _absolute(path).resolve()
    if not root.is_dir():
        raise OKFError(f"{root} is not a folder")
    current = [str(p) for p in bundles(home)]
    store.save_config(home, {**store.load_config(home),
                             "okf_bundles": current if str(root) in current else [*current, str(root)]})
    return root


def remove_bundle(home: Path, path: str) -> bool:
    root = str(Path(path).expanduser().resolve())
    current = [str(p) for p in bundles(home)]
    store.save_config(home, {**store.load_config(home), "okf_bundles": [p for p in current if p != root]})
    return root in current


# ---------- reading ----------

def _flat(value: object) -> object:
    """A scalar, or a mapping of scalars; anything deeper is dropped (no alias bombs, no odd types)."""
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if isinstance(v, _SCALARS)}
    return None


def _clean(front: dict) -> dict:
    """Frontmatter keeps scalars, flat mappings and lists of those: all the spec's fields fit."""
    clean: dict = {}
    for key, value in front.items():
        if isinstance(value, list):
            clean[str(key)] = [v for v in map(_flat, value) if v is not None]
        elif (flat := _flat(value)) is not None:
            clean[str(key)] = flat
    return clean


def _parse(path: Path) -> tuple[dict | None, str]:
    """(frontmatter, body), cached by mtime and size. One odd or hostile page never breaks the others."""
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        if (hit := _CACHE.get(str(path))) and hit[0] == stamp:
            return hit[1]
        if st.st_size > MAX_PAGE_BYTES:
            return None, ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    match = _FRONT.match(text)
    result: tuple[dict | None, str] = (None, text)
    if match:
        try:
            front = yaml.safe_load(match.group(1))
            result = (_clean(front) if isinstance(front, dict) else None), text[match.end():].strip()
        except Exception:  # noqa: BLE001 - YAMLError, ValueError (bad dates), RecursionError (deep nesting)
            result = None, text
    _CACHE[str(path)] = (stamp, result)
    return result


def _markdown_files(root: Path) -> list[Path]:
    """.md files under root, skipping dot folders (.git) and links that point outside the bundle."""
    real_root = root.resolve()
    files = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not _isjunction(os.path.join(folder, d)))
        for name in sorted(names):
            path = Path(folder) / name
            if name.startswith(".") or not name.endswith(".md"):
                continue
            if path.is_symlink() and real_root not in path.resolve().parents:
                continue
            files.append(path)
            if len(files) >= MAX_FILES:
                return files
    return files


def concepts(root: Path) -> list[dict]:
    found = []
    for path in _markdown_files(root):
        if path.stem in RESERVED:
            continue
        front, body = _parse(path)
        if front and str(front.get("type") or "").strip():
            cid = path.relative_to(root).with_suffix("").as_posix()
            found.append({"concept_id": cid, "path": path, "front": front, "body": body, "bundle": str(root)})
    return found


def problems(root: Path) -> list[str]:
    """Conformance problems (§11): non-reserved .md files without frontmatter carrying a `type`."""
    issues = []
    for path in _markdown_files(root):
        if path.stem in RESERVED:
            continue
        front, _ = _parse(path)
        rel = path.relative_to(root).as_posix()
        if path.stat().st_size > MAX_PAGE_BYTES:
            issues.append(f"{rel}: larger than {MAX_PAGE_BYTES // 1024} KB (ignored)")
        elif not front or not str(front.get("type") or "").strip():
            issues.append(f"{rel}: no YAML frontmatter with a 'type'")
    return issues


def _verifications(front: dict) -> list[dict]:
    value = front.get("verified")
    if isinstance(value, dict):  # §5.2: a bare mapping is a one-element list
        return [value]
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def trust_tier(front: dict) -> str:
    """§5.3: unverified < machine-confirmed < human-reviewed."""
    actors = [str(v.get("by", "")) for v in _verifications(front)]
    if not actors:
        return "unverified"
    return "human-reviewed" if any(a.startswith("human:") for a in actors) else "machine-confirmed"


def _status(front: dict) -> str:
    return str(front.get("status") or "stable").strip().lower()


def is_stale(front: dict) -> bool:
    value = front.get("stale_after")
    if not value:
        return False
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return False
    return datetime.now(UTC) >= (when if when.tzinfo else when.replace(tzinfo=UTC))


_TRUST_RANK = {"human-reviewed": 2, "machine-confirmed": 1, "unverified": 0}


def _summary(c: dict) -> dict:
    front = c["front"]
    return {"concept_id": c["concept_id"], "bundle": c["bundle"], "type": front.get("type"),
            "title": front.get("title") or Path(c["concept_id"]).name, "description": front.get("description"),
            "tags": front.get("tags") or [], "trust": trust_tier(front), "status": _status(front),
            "stale": is_stale(front)}


def search(home: Path, query: str, limit: int = 5, include_deprecated: bool = False) -> list[dict]:
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
    if not words:
        return []
    scored = []
    for root in bundles(home):
        if not root.is_dir():
            continue
        for c in concepts(root):
            if _status(c["front"]) == "deprecated" and not include_deprecated:
                continue
            front = c["front"]
            fields = [(3, str(front.get("title") or "") + " " + c["concept_id"]),
                      (3, " ".join(map(str, front.get("tags") or []))),
                      (2, str(front.get("description") or "")), (1, c["body"])]
            score = sum(weight * text.lower().count(w) for w in words for weight, text in fields)
            if score:
                hit = _summary(c)
                snippet = next((ln.strip() for ln in c["body"].splitlines() if any(w in ln.lower() for w in words)), "")
                scored.append((score + _TRUST_RANK[hit["trust"]], {**hit, "snippet": snippet[:240]}))
    return [h for _, h in sorted(scored, key=lambda s: -s[0])[:limit]]


def _safe_id(concept_id: str) -> str:
    """Reject anything that is not a plain relative path before it reaches the filesystem (no UNC, drives, ..)."""
    cid = concept_id.strip().removesuffix(".md").lstrip("/")
    if not cid or any(c in cid for c in "\\:\x00") or any(p in ("", ".", "..") for p in cid.split("/")):
        raise OKFError("concept_id must be a path inside the bundle, like 'pricing/rider_rent'")
    return cid


def _resolve(home: Path, concept_id: str, bundle: str = "") -> tuple[Path, Path]:
    roots = [Path(bundle).resolve()] if bundle else bundles(home)
    if bundle and str(roots[0]) not in [str(p) for p in bundles(home)]:
        raise OKFError(f"{bundle} is not a configured bundle; add it with /memory-okf add <folder>")
    if not roots:
        raise OKFError("no OKF bundle configured; add one with /memory-okf add <folder>")
    for root in roots:
        path = (root / f"{concept_id}.md").resolve()
        if root.resolve() not in path.parents:
            raise OKFError("concept_id must stay inside the bundle")
        if path.exists():
            return root, path
    raise OKFError(f"no concept {concept_id!r} in the configured bundles")


def read(home: Path, concept_id: str, bundle: str = "") -> dict:
    try:
        root, path = _resolve(home, _safe_id(concept_id), bundle)
    except OKFError as exc:
        return {"error": str(exc)}
    front, body = _parse(path)
    if not front:
        return {"error": f"{concept_id} has no valid OKF frontmatter"}
    cid = path.relative_to(root).with_suffix("").as_posix()
    return {**_summary({"concept_id": cid, "front": front, "body": body, "bundle": str(root)}),
            "body": body, "links": _LINK.findall(body), "sources": front.get("sources") or []}


# ---------- draft write-back ----------

def _validate_draft(home: Path, args: dict) -> tuple[Path, str]:
    cid = str(args.get("concept_id") or "").strip()
    parts = cid.split("/")
    if (not cid or any(not _SEGMENT.match(p) or p in _WIN_DEVICES for p in parts) or parts[-1] in RESERVED):
        raise OKFError("concept_id must be lowercase path segments like 'centre/address' "
                       "(not index, log or a Windows device name such as con)")
    if not str(args.get("type") or "").strip():
        raise OKFError("type is required (for example Reference, Metric, Playbook)")
    if not str(args.get("title") or "").strip():
        raise OKFError("title is required")
    text = " ".join(str(args.get(k) or "") for k in ("title", "description", "body"))
    reasons = scan(text)
    if reasons:
        raise OKFError(f"blocked: {'; '.join(reasons)}")
    roots = bundles(home)
    bundle = str(args.get("bundle") or "")
    root = Path(bundle).resolve() if bundle else (roots[0] if roots else None)
    if root is None:
        raise OKFError("no OKF bundle configured; add one with /memory-okf add <folder>")
    if str(root) not in [str(p) for p in roots]:
        raise OKFError(f"{root} is not a configured bundle")
    if (root / f"{cid}.md").exists():
        raise OKFError(f"{cid} already exists; drafts never overwrite existing pages")
    return root, cid


def submit_draft(home: Path, args: dict) -> dict:
    try:
        root, cid = _validate_draft(home, args)
    except OKFError as exc:
        history.record(home, "blocked" if str(exc).startswith("blocked") else "refused", target="okf",
                       detail=str(args.get("concept_id")), reason=str(exc))
        return {"success": False, "error": str(exc)}
    op = {"concept_id": cid, "bundle": str(root), "type": str(args["type"]).strip(),
          "title": str(args["title"]).strip(), "description": str(args.get("description") or "").strip(),
          "body": str(args.get("body") or "").strip(),
          "tags": [str(t) for t in (args.get("tags") or []) if str(t).strip()]}
    with store.locked(home):
        return store.stage(home, "okf", [op], [], "OKF drafts are written only after /memory-approve")


def _inline(text: str) -> str:
    """One markdown line: no newlines, and no brackets that could forge a link or entry."""
    return re.sub(r"([\[\]()])", r"\\\1", " ".join(str(text).split()))


def _append_log(root: Path, line: str) -> None:
    path, today = root / "log.md", datetime.now(UTC).strftime("%Y-%m-%d")
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else "# Directory Update Log\n"
    heading = f"## {today}"
    if heading in text:
        text = text.replace(heading, f"{heading}\n* {line}", 1)
    elif "\n## " in text:
        i = text.index("\n## ")
        text = f"{text[:i].rstrip()}\n\n{heading}\n* {line}\n{text[i:]}"
    else:
        text = f"{text.rstrip()}\n\n{heading}\n* {line}\n"
    store.write_text(path, text)


def apply_staged(home: Path, item: dict) -> str | None:
    """Write an approved draft page. Returns an error message, or None on success."""
    op = item["operations"][0]
    root = Path(op["bundle"])
    path = root / f"{op['concept_id']}.md"
    if not root.is_dir():
        return f"bundle {root} no longer exists"
    if path.exists():
        return f"{op['concept_id']} was created meanwhile; drafts never overwrite"
    if root.resolve() not in path.resolve().parents:  # a linked sub-folder must not lead outside the bundle
        return f"{op['concept_id']} would be written outside the bundle"
    front = {"type": op["type"], "title": op["title"]}
    if op["description"]:
        front["description"] = op["description"]
    if op["tags"]:
        front["tags"] = op["tags"]
    front.update({"status": "draft", "generated": {"by": _actor(), "at": _now()}})
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(path, f"---\n{yaml.safe_dump(front, sort_keys=False, allow_unicode=True)}---\n\n{op['body']}\n")
    link = f"/{op['concept_id']}.md"
    title = _inline(op["title"])
    _append_log(root, f"**Creation**: Drafted [{title}]({link}) (status: draft, awaiting human review).")
    index = path.parent / "index.md"
    if index.exists():
        desc = f" - {_inline(op['description'])}" if op["description"] else ""
        current = index.read_text(encoding="utf-8", errors="replace").rstrip()
        store.write_text(index, current + f"\n* [{title}]({path.name}){desc}\n")
    return None


# ---------- export ----------

def _concept_text(front: dict, body: str) -> str:
    return f"---\n{yaml.safe_dump(front, sort_keys=False, allow_unicode=True)}---\n\n{body.strip()}\n"


def _skill_pages(home: Path) -> list[tuple[str, dict, str]]:
    from . import skills  # local: skills imports store, keep okf importable on its own

    pages = []
    root = skills.skills_dir()
    for name in skills.owned(home):
        front, body = _parse(root / name / "SKILL.md")
        pages.append((name, front or {}, body))
    return pages


def _remove_previous_export(out: Path) -> None:
    """Delete only the files the last export listed in its marker; pages the user added stay."""
    marker = out / EXPORT_MARKER
    if not marker.exists():
        return
    real_out = out.resolve()
    for rel in marker.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
        path = out / rel.strip()
        if rel.strip() and path.is_file() and not path.is_symlink() and real_out in path.resolve().parents:
            path.unlink()


def export(home: Path, out_dir: Path) -> dict:
    """Write the notebook and the plugin's skills as an OKF v0.2 bundle (never includes the chat archive)."""
    out = _absolute(out_dir)
    if out.exists() and any(out.iterdir()) and not (out / EXPORT_MARKER).exists():
        raise OKFError(f"{out} already has other files; export into an empty folder")
    _remove_previous_export(out)
    written: list[str] = []

    def write(rel: str, text: str) -> None:
        store.write_text(out / rel, redact(text))  # rescan on the way out: notes may predate a pattern
        written.append(rel)

    generated = {"by": _actor(), "at": _now()}
    notebooks = [("user-profile", "User profile", "User Profile", "user",
                  "Who the user is and how they like to work (hermes-memory USER.md)."),
                 ("work-notes", "Work notes", "Notes", "memory",
                  "Durable lessons about the user's work, shared across projects (hermes-memory MEMORY.md).")]
    index: dict[str, list[tuple[str, str, str]]] = {"memory": [], "skills": []}
    for slug, title, kind, target, desc in notebooks:
        entries = store.read_entries(home, target)
        body = "\n".join(f"- {' '.join(e.split())}" for e in entries) or "_No entries yet._"
        write(f"memory/{slug}.md", _concept_text(
            {"type": kind, "title": title, "description": desc, "tags": ["hermes-memory"], "generated": generated},
            body))
        index["memory"].append((title, f"{slug}.md", desc))
    for name, front, body in _skill_pages(home):
        desc = str(front.get("description") or "")
        write(f"skills/{name}.md", _concept_text(
            {"type": "Playbook", "title": name, "description": desc, "tags": ["skill", "hermes-memory"],
             "generated": generated}, body))
        index["skills"].append((name, f"{name}.md", desc))
    for sub, items in index.items():
        lines = [f"# {sub.capitalize()}", ""] + [f"* [{_inline(t)}]({href}) - {_inline(d)}" for t, href, d in items]
        write(f"{sub}/index.md", "\n".join(lines) + "\n")
    write("index.md", f'---\nokf_version: "{OKF_VERSION}"\n---\n\n# hermes-memory export\n\n'
          "* [Memory](memory/) - the shared notebook\n* [Skills](skills/) - reusable procedures\n")
    _append_log(out, f"**Update**: Exported {len(index['memory'])} notebooks and {len(index['skills'])} skills.")
    store.write_text(out / EXPORT_MARKER, "Written by hermes-memory /memory-export-okf. Files it wrote:\n"
                     + "".join(f"{rel}\n" for rel in written))
    history.record(home, "exported", target="okf", detail=str(out))
    return {"path": str(out), "concepts": len(index["memory"]) + len(index["skills"])}


def render_bundles(home: Path) -> str:
    roots = bundles(home)
    if not roots:
        return "No OKF bundles configured. Add one with: /memory-okf add <folder>"
    lines = []
    for root in roots:
        if not root.is_dir():
            lines.append(f"MISSING  {root}")
            continue
        found, issues = concepts(root), problems(root)
        lines.append(f"OK       {root}  ({len(found)} pages, {len(issues)} conformance problems)")
    return "\n".join(lines)
