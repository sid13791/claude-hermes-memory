"""Chat archive: Claude Code transcripts copied into SQLite with FTS5 full-text search.

Claude Code deletes old transcripts (30 days by default). This archive keeps the conversation text
(user + assistant messages only, secrets redacted) so `recall` can answer "what did we decide?"
weeks later. Indexing is incremental: each transcript file remembers the byte offset already read.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from .security import redact

DB_FILE = "archive.db"
CHUNK_BYTES = 4 * 1024 * 1024  # per transaction, so one huge transcript cannot blow a hook's budget
_SYSTEM_BLOCKS = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, project TEXT, first_ts TEXT, last_ts TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, uuid TEXT UNIQUE, ts TEXT, role TEXT, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def transcripts_root() -> Path:
    override = os.environ.get("HERMES_MEMORY_TRANSCRIPTS")
    if override:
        return Path(override)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config_dir) if config_dir else Path.home() / ".claude") / "projects"


@contextlib.contextmanager
def _db(home: Path) -> Iterator[sqlite3.Connection]:
    """One connection in autocommit mode; writers take an explicit BEGIN IMMEDIATE."""
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / DB_FILE, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


# ---------- parsing ----------

def _text_of(content: object) -> str:
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):  # keep text blocks; drop thinking, tool_use, tool_result
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    else:
        return ""
    return _SYSTEM_BLOCKS.sub("", "\n".join(parts)).strip()


def _message_from(record: dict) -> dict | None:
    if record.get("type") not in ("user", "assistant") or record.get("isSidechain"):
        return None
    msg = record.get("message")
    if not isinstance(msg, dict):
        return None
    text = _text_of(msg.get("content"))
    if not text:
        return None
    cwd = record.get("cwd") or ""
    session = str(record.get("sessionId") or "unknown")
    text = redact(text)
    uuid = record.get("uuid") or hashlib.sha1(  # a dedupe key, not security
        f"{session}|{record.get('timestamp')}|{record['type']}|{text}".encode()).hexdigest()
    return {"session_id": session, "uuid": uuid, "ts": record.get("timestamp"), "role": record["type"],
            "text": text, "project": Path(cwd.replace("\\", "/")).name if cwd else ""}


# ---------- indexing ----------

def _stored_offset(conn: sqlite3.Connection, path: Path) -> int:
    row = conn.execute("SELECT offset FROM files WHERE path = ?", (str(path),)).fetchone()
    return row["offset"] if row else 0


def _read_complete_lines(path: Path, offset: int, max_bytes: int) -> bytes:
    """Up to ``max_bytes`` from ``offset``, cut back to the last newline (a line longer than the cap
    is read whole). A half-written last line is left for the next run."""
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(max_bytes)
        while chunk and b"\n" not in chunk:
            more = fh.read(max_bytes)
            if not more:
                return b""
            chunk += more
    end = chunk.rfind(b"\n")
    return chunk[: end + 1] if end >= 0 else b""


def _index_chunk(conn: sqlite3.Connection, path: Path, max_bytes: int) -> tuple[int, bool]:
    """Index one bounded chunk in its own transaction. Returns (messages added, file fully read)."""
    size = path.stat().st_size
    conn.execute("BEGIN IMMEDIATE")  # serialises the Stop hook, SessionStart sweep and recall
    try:
        offset = _stored_offset(conn, path)
        if size < offset:  # transcript was truncated or replaced: start again
            offset = 0
        chunk = _read_complete_lines(path, offset, max_bytes) if size > offset else b""
        added = 0
        for raw in chunk.splitlines():
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            msg = _message_from(record) if isinstance(record, dict) else None
            if msg and _insert(conn, msg):
                added += 1
        new_offset = offset + len(chunk)
        conn.execute("INSERT INTO files(path, offset) VALUES (?, ?) "
                     "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset", (str(path), new_offset))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return added, (not chunk) or new_offset >= size


def index_file(home: Path, path: Path | str, max_bytes: int = CHUNK_BYTES) -> int:
    """Index the next bounded chunk of one transcript. Returns the number of messages added."""
    path = Path(path)
    if not path.exists():
        return 0
    with _db(home) as conn:
        return _index_chunk(conn, path, max_bytes)[0]


def _insert(conn: sqlite3.Connection, msg: dict) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO messages(session_id, uuid, ts, role, text) VALUES (?, ?, ?, ?, ?)",
                       (msg["session_id"], msg["uuid"], msg["ts"], msg["role"], msg["text"]))
    if cur.rowcount == 0:  # same message already archived (e.g. a resumed or copied transcript)
        return False
    conn.execute("""INSERT INTO sessions(session_id, project, first_ts, last_ts) VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                      last_ts = max(coalesce(last_ts, ''), coalesce(excluded.last_ts, '')),
                      project = coalesce(nullif(project, ''), excluded.project)""",
                 (msg["session_id"], msg["project"], msg["ts"], msg["ts"]))
    return True


def sweep(home: Path, budget_seconds: float | None = None) -> dict:
    """Index new data in every transcript, newest first, stopping when the time budget runs out."""
    started = time.monotonic()
    stats = {"files": 0, "messages": 0, "skipped_for_time": 0}
    files = sorted(transcripts_root().glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    with _db(home) as conn:
        known = {r["path"]: r["offset"] for r in conn.execute("SELECT path, offset FROM files")}
        for path in files:
            if path.stat().st_size == known.get(str(path)):
                continue  # unchanged since last time: no work
            if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                stats["skipped_for_time"] += 1
                continue
            stats["files"] += 1
            done = False
            while not done:
                added, done = _index_chunk(conn, path, CHUNK_BYTES)
                stats["messages"] += added
                if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                    break
    return stats


def last_assistant_text(path: Path | str, tail_bytes: int = 256 * 1024) -> str:
    """Text of Claude's most recent reply in a transcript (read from the end of the file)."""
    path = Path(path)
    if not path.exists():
        return ""
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - tail_bytes))
        lines = fh.read().splitlines()
    for raw in reversed(lines):
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict) and record.get("type") == "assistant" and not record.get("isSidechain"):
            text = _text_of((record.get("message") or {}).get("content"))
            if text:
                return text
    return ""


# ---------- reading ----------

def _fts_query(query: str, joiner: str) -> str:
    words = re.findall(r"\w+", query)
    return f" {joiner} ".join(f'"{w}"' for w in words)


def search(home: Path, query: str, limit: int = 5, project: str = "") -> list[dict]:
    """Full-text search: every word must match; if nothing does, any word may match."""
    if not re.search(r"\w", query or ""):
        return []
    sql = """SELECT m.id, m.session_id, m.ts, m.role, s.project,
                    snippet(messages_fts, 0, '[', ']', '…', 24) AS snippet
             FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
             JOIN sessions s ON s.session_id = m.session_id
             WHERE messages_fts MATCH ? AND (? = '' OR instr(lower(s.project), lower(?)) > 0)
             ORDER BY bm25(messages_fts) LIMIT ?"""
    with _db(home) as conn:
        for joiner in ("AND", "OR"):
            rows = conn.execute(sql, (_fts_query(query, joiner), project, project, limit)).fetchall()
            if rows:
                return [{"message_id": r["id"], "session_id": r["session_id"], "ts": r["ts"], "role": r["role"],
                         "project": r["project"], "snippet": r["snippet"]} for r in rows]
    return []


def scroll(home: Path, session_id: str, around_id: int, window: int = 6) -> list[dict]:
    with _db(home) as conn:
        before = conn.execute("SELECT id, ts, role, text FROM messages WHERE session_id = ? AND id <= ? "
                              "ORDER BY id DESC LIMIT ?", (session_id, around_id, window + 1)).fetchall()
        after = conn.execute("SELECT id, ts, role, text FROM messages WHERE session_id = ? AND id > ? "
                             "ORDER BY id LIMIT ?", (session_id, around_id, window)).fetchall()
    return [dict(r) for r in [*reversed(before), *after]]


def session_messages(home: Path, session_id: str) -> list[dict]:
    with _db(home) as conn:
        rows = conn.execute("SELECT id, ts, role, text FROM messages WHERE session_id = ? ORDER BY id",
                            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def stats(home: Path) -> dict:
    with _db(home) as conn:
        sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        oldest = conn.execute("SELECT min(first_ts) FROM sessions").fetchone()[0]
    return {"sessions": sessions, "messages": messages, "oldest": oldest}


def render_hits(hits: list[dict]) -> str:
    if not hits:
        return "No past conversations matched."
    return "\n".join(f"{(h['ts'] or '?')[:10]}  {h['project'] or '?':<28} {h['role']:<9} {' '.join(h['snippet'].split())}\n"
                     f"            session {h['session_id']} · message {h['message_id']}" for h in hits)
