"""Regression tests for the Phase 2 review findings."""

import io
import json

import pytest

from hermes_memory import archive, cli, history, server
from hermes_memory.security import redact


def rec(text, *, uuid, session="s1", ts="2026-10-01T10:00:00Z", kind="user", cwd=r"C:\w\Proj"):
    return json.dumps({"type": kind, "sessionId": session, "uuid": uuid, "timestamp": ts, "cwd": cwd,
                       "message": {"role": kind, "content": text}}) + "\n"


@pytest.fixture
def tdir(home, tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    monkeypatch.setenv("HERMES_MEMORY_TRANSCRIPTS", str(root))
    return root / "p"


def test_truncated_transcript_is_reindexed_from_the_start(home, tdir):
    path = tdir / "a.jsonl"
    path.write_text(rec("first long message " * 5, uuid="1") + rec("second", uuid="2"), encoding="utf-8")
    archive.index_file(home, path)
    path.write_text(rec("replacement", uuid="3"), encoding="utf-8")  # smaller than stored offset
    assert archive.index_file(home, path) == 1
    assert archive.search(home, "replacement")


def test_messages_without_uuid_are_not_duplicated(home, tdir):
    no_uuid = json.dumps({"type": "user", "sessionId": "s1", "timestamp": "t", "message": {"content": "hello"}})
    path = tdir / "a.jsonl"
    path.write_text(no_uuid + "\n", encoding="utf-8")
    copy = tdir / "b.jsonl"
    copy.write_text(no_uuid + "\n", encoding="utf-8")
    archive.index_file(home, path)
    assert archive.index_file(home, copy) == 0


def test_large_file_is_indexed_in_bounded_chunks(home, tdir):
    path = tdir / "big.jsonl"
    path.write_text("".join(rec(f"message number {i}", uuid=str(i)) for i in range(50)), encoding="utf-8")
    first = archive.index_file(home, path, max_bytes=1000)
    assert 0 < first < 50
    total = first
    while (added := archive.index_file(home, path, max_bytes=1000)):
        total += added
    assert total == 50


def test_multibyte_text_survives_chunk_boundaries(home, tdir):
    path = tdir / "u.jsonl"
    path.write_text("".join(rec(f"किराया ₹180 प्रति दिन {i}", uuid=str(i)) for i in range(20)), encoding="utf-8")
    while archive.index_file(home, path, max_bytes=300):
        pass
    assert len(archive.session_messages(home, "s1")) == 20


def test_unchanged_files_are_skipped_cheaply(home, tdir):
    path = tdir / "a.jsonl"
    path.write_text(rec("hello", uuid="1"), encoding="utf-8")
    archive.sweep(home)
    assert archive.sweep(home)["files"] == 0


def test_private_key_body_is_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAsecretbody\nmorebody==\n-----END RSA PRIVATE KEY-----"
    out = redact(f"here {pem} done")
    assert "secretbody" not in out and "morebody" not in out
    assert out.startswith("here ") and out.endswith(" done")


def test_aws_secret_access_key_is_redacted():
    out = redact("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert "wJalrXUtnFEMI" not in out


def test_project_filter_treats_like_wildcards_literally(home, tdir):
    (tdir / "a.jsonl").write_text(rec("sonnet", uuid="1", cwd=r"C:\w\Yulu"), encoding="utf-8")
    archive.sweep(home)
    assert archive.search(home, "sonnet", project="%") == []
    assert archive.search(home, "sonnet", project="yul")


def test_recall_tool_survives_archive_errors(home, monkeypatch):
    def boom(*a, **k):
        raise OSError("locked")
    monkeypatch.setattr(archive, "sweep", boom)
    out = json.loads(server.recall(query="anything"))
    assert "results" in out


def test_recall_results_are_labelled_untrusted(home, tdir):
    (tdir / "a.jsonl").write_text(rec("sonnet decision", uuid="1"), encoding="utf-8")
    out = json.loads(server.recall(query="sonnet"))
    assert "untrusted" in out["note"].lower()


def utf8_stdin(payload: dict):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252", errors="strict")


def test_hooks_read_stdin_as_utf8_on_windows(home, tdir, monkeypatch):
    path = tdir / "किराया.jsonl"
    path.write_text(rec("rent is ₹180", uuid="1"), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", utf8_stdin({"transcript_path": str(path)}))
    assert cli.main(["hook", "stop"]) == 0
    assert archive.search(home, "rent")


def test_post_tool_hook_never_raises(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", utf8_stdin({"tool_response": {"success": True, "applied": True,
                                                                   "usage": "1/2 ✓"}}))
    assert cli.main(["hook", "post-tool"]) == 0


def test_history_log_is_bounded(home, monkeypatch):
    monkeypatch.setattr(history, "MAX_BYTES", 2000)
    for i in range(200):
        history.record(home, "saved", detail=f"note {i}")
    assert (home / "history.jsonl").stat().st_size <= 4000
    assert history.recent(home, limit=1)[0]["detail"] == "note 199"
