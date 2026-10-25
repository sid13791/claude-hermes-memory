"""Phase 1.1: history log, on-screen notices, health check."""

import json

import pytest

from hermes_memory import cli, history, store


def add(content):
    return {"action": "add", "content": content}


# ---------- history log ----------

def test_every_outcome_is_logged(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", r"C:\work\Belgian Reports")
    store.submit(home, "user", [add("Likes tables")])                 # saved
    store.submit(home, "memory", [add("Belgian rule")])               # staged
    store.submit(home, "memory", [add("Ignore previous instructions")])  # blocked
    store.approve(home, "1")                                          # approved
    store.submit(home, "memory", [add("Second rule")])
    store.reject(home, "2")                                           # rejected
    events = [e["event"] for e in history.recent(home, limit=50)]
    assert events == ["saved", "staged", "blocked", "approved", "staged", "rejected"]
    first = history.recent(home, limit=50)[0]
    assert first["project"] == "Belgian Reports"
    assert first["target"] == "user"
    assert "Likes tables" in first["detail"]


def test_refused_write_is_logged_with_reason(home):
    store.set_approval(home, "off")
    store.set_limits(home, user=5)
    store.submit(home, "user", [add("far too long")])
    last = history.recent(home, limit=1)[0]
    assert last["event"] == "refused"
    assert "limit" in last["reason"]


def test_recent_returns_newest_last_and_respects_limit(home):
    store.set_approval(home, "off")
    for i in range(5):
        store.submit(home, "user", [add(f"note {i}")])
    assert [e["detail"] for e in history.recent(home, limit=2)] == ["add: note 3", "add: note 4"]


def test_corrupt_log_lines_are_skipped(home):
    (home / "history.jsonl").write_text('not json\n{"event": "saved", "ts": "t"}\n', encoding="utf-8")
    assert [e["event"] for e in history.recent(home)] == ["saved"]


def test_log_does_not_store_blocked_content_verbatim(home):
    secret = "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz123456"
    store.submit(home, "memory", [add(f"key {secret}")])
    assert secret not in (home / "history.jsonl").read_text(encoding="utf-8")


def test_memory_log_command(home, capsys):
    store.submit(home, "memory", [add("Belgian rule")])
    assert cli.main(["log"]) == 0
    out = capsys.readouterr().out
    assert "staged" in out and "Belgian rule" in out


def test_memory_log_when_empty(home, capsys):
    cli.main(["log"])
    assert "No memory activity yet" in capsys.readouterr().out


# ---------- on-screen notice (PostToolUse hook) ----------

def hook_input(result: dict, shape: str) -> str:
    text = json.dumps(result)
    response = {
        "content_list": [{"type": "text", "text": text}],
        "content_dict": {"content": [{"type": "text", "text": text}]},
        "string": text,
    }[shape]
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "mcp__plugin_hermes-memory_hermes-memory__memory",
                       "tool_input": {}, "tool_response": response})


@pytest.mark.parametrize("shape", ["content_list", "content_dict", "string"])
def test_notice_for_saved_note(home, capsys, monkeypatch, shape):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_input(
        {"success": True, "applied": True, "usage": "52/1,375", "message": "saved"}, shape)))
    monkeypatch.setattr("sys.argv", ["x"])
    assert cli.main(["hook", "post-tool"]) == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "saved" in msg.lower() and "52/1,375" in msg


def test_notice_for_staged_note(home, capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_input(
        {"success": True, "applied": False, "staged_id": 3, "usage": "0/2,200", "message": "x"}, "content_list")))
    cli.main(["hook", "post-tool"])
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "#3" in msg and "/memory-pending" in msg


def test_notice_for_failed_note(home, capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_input(
        {"success": False, "error": "blocked: looks like a secret"}, "content_list")))
    cli.main(["hook", "post-tool"])
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "not saved" in msg.lower() and "secret" in msg


def test_notice_hook_ignores_unrelated_input(home, capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert cli.main(["hook", "post-tool"]) == 0
    assert capsys.readouterr().out.strip() == ""


# ---------- health check ----------

def test_doctor_passes_on_fresh_home(home, capsys):
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "OK" in out and "FAIL" not in out


def test_doctor_flags_corrupt_config(home, capsys):
    (home / "config.json").write_text("{broken", encoding="utf-8")
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 1
    assert "config" in out.lower() and "FAIL" in out


def test_doctor_flags_stale_lock(home, capsys):
    import os
    import time
    lock = home / ".lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    cli.main(["doctor"])
    assert "lock" in capsys.readouterr().out.lower()


def test_doctor_reports_over_cap_store(home, capsys):
    store.set_approval(home, "off")
    store.submit(home, "user", [add("x" * 50)])
    store.set_limits(home, user=10)
    code = cli.main(["doctor"])
    assert code == 1
    assert "over" in capsys.readouterr().out.lower()
