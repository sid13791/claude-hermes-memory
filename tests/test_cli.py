import json

from hermes_memory import cli, store


def run(capsys, *args):
    code = cli.main(list(args))
    return code, capsys.readouterr().out


def test_session_start_emits_hook_json_with_snapshot(home, capsys):
    store.set_approval(home, "off")
    store.submit(home, "user", [{"action": "add", "content": "Prefers tables"}])
    code, out = run(capsys, "session-start")
    payload = json.loads(out)
    assert code == 0
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Prefers tables" in payload["hookSpecificOutput"]["additionalContext"]


def test_show_prints_both_stores(home, capsys):
    code, out = run(capsys, "show")
    assert code == 0
    assert "USER PROFILE" in out and "MEMORY" in out


def test_pending_approve_reject_flow(home, capsys):
    store.submit(home, "memory", [{"action": "add", "content": "Belgian rule"}])
    _, out = run(capsys, "pending")
    assert "#1" in out and "Belgian rule" in out
    code, out = run(capsys, "approve", "1")
    assert code == 0 and "Approved" in out
    _, out = run(capsys, "pending")
    assert "No memories waiting" in out


def test_approve_failure_returns_nonzero(home, capsys):
    code, _out = run(capsys, "approve", "9")
    assert code == 1


def test_approval_mode_show_and_set(home, capsys):
    _, out = run(capsys, "approval")
    assert "auto" in out
    code, _ = run(capsys, "approval", "on")
    assert code == 0 and store.load_config(home)["approval"] == "on"
    code, _ = run(capsys, "approval", "maybe")
    assert code == 1
