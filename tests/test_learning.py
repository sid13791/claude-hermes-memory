"""Phase 3: in-chat yes/no approval and the learning nudge.

A question only counts once Claude has actually shown it: the Stop hook reads Claude's last reply from
the transcript and "arms" the item for that session. The user's next prompt in the same session, within
the time limit, answers it.
"""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from hermes_memory import cli, learning, store


def add(content):
    return {"action": "add", "content": content}


def stdin(monkeypatch, payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))


def prompt_hook(monkeypatch, capsys, text, session="sess-1"):
    stdin(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text})
    assert cli.main(["hook", "prompt"]) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


@pytest.fixture
def show(home, tmp_path, monkeypatch, capsys):
    """Simulate Claude finishing a reply whose text is ``reply`` (the Stop hook reads it)."""
    def _show(reply, session="sess-1"):
        path = tmp_path / f"{session}.jsonl"
        record = {"type": "assistant", "sessionId": session, "uuid": f"u{datetime.now().timestamp()}",
                  "timestamp": "2026-10-25T10:00:00Z", "message": {"role": "assistant",
                                                                    "content": [{"type": "text", "text": reply}]}}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        stdin(monkeypatch, {"session_id": session, "transcript_path": str(path)})
        assert cli.main(["hook", "stop"]) == 0
        capsys.readouterr()
    return _show


def stage_and_show(home, show, content, session="sess-1"):
    res = store.submit(home, "memory", [add(content)])
    show(f"Done with the report.\n\n{res['question']}", session=session)
    return res


def context(result):
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


# ---------- the tool tells Claude to ask, once ----------

def test_first_staged_note_carries_a_question(home):
    res = store.submit(home, "memory", [add("Belgian P&L shows % of revenue")])
    assert res["ask_user"] is True
    assert "#1" in res["question"] and "yes" in res["question"].lower()


def test_second_staged_note_waits_without_asking(home):
    store.submit(home, "memory", [add("one")])
    assert store.submit(home, "memory", [add("two")])["ask_user"] is False


# ---------- reply classification ----------

@pytest.mark.parametrize("text, kind", [
    ("yes", "yes"), ("Yes!", "yes"), ("y", "yes"), ("ok", "yes"), ("save it", "yes"), ("haan", "yes"),
    ("no", "no"), ("Nope.", "no"), ("don't save", "no"), ("nahi", "no"), ("reject", "no"),
    ("yes, but only for Meerut", "change"), ("change it to Jul'26 only", "change"),
    ("no, make it Jul'26 only", "change"),
    ("what is the P&L for September?", "other"), ("ok now run the tests", "other"),
    ("yes and also export to Excel", "other"), ("only the first file", "other"),
])
def test_classify_reply(text, kind):
    assert learning.classify(text)[0] == kind


# ---------- the hook acts on the user's real reply ----------

def test_yes_approves_the_shown_note(home, show, monkeypatch, capsys):
    stage_and_show(home, show, "Belgian P&L shows % of revenue")
    result = prompt_hook(monkeypatch, capsys, "yes")
    assert store.read_entries(home, "memory") == ["Belgian P&L shows % of revenue"]
    assert "approved" in context(result).lower() and "#1" in result["systemMessage"]


def test_yes_does_nothing_if_the_question_was_never_shown(home, monkeypatch, capsys):
    store.submit(home, "memory", [add("never shown")])
    assert prompt_hook(monkeypatch, capsys, "yes") == {}
    assert store.read_entries(home, "memory") == []


def test_question_buried_mid_reply_does_not_arm(home, show, monkeypatch, capsys):
    res = store.submit(home, "memory", [add("buried")])
    show(f"{res['question']}\n\n" + "Also, shall I run the tests now? " * 30)
    prompt_hook(monkeypatch, capsys, "yes")
    assert store.read_entries(home, "memory") == []


def test_yes_in_another_session_does_not_approve(home, show, monkeypatch, capsys):
    stage_and_show(home, show, "rule from session A", session="A")
    prompt_hook(monkeypatch, capsys, "yes", session="B")
    assert store.read_entries(home, "memory") == []


def test_stale_question_expires(home, show, monkeypatch, capsys):
    stage_and_show(home, show, "old rule")
    pending = store._load_pending(home)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    pending["items"][0]["armed"]["at"] = old
    store._save_pending(home, pending)
    prompt_hook(monkeypatch, capsys, "yes")
    assert store.read_entries(home, "memory") == []


def test_no_rejects_the_shown_note(home, show, monkeypatch, capsys):
    stage_and_show(home, show, "wrong guess")
    result = prompt_hook(monkeypatch, capsys, "no")
    assert store.list_pending(home) == [] and store.read_entries(home, "memory") == []
    assert "declined" in context(result).lower()


def test_change_discards_and_asks_claude_to_restage(home, show, monkeypatch, capsys):
    stage_and_show(home, show, "Electricity: always bank figure")
    result = prompt_hook(monkeypatch, capsys, "yes, but only for Jul'26")
    assert store.list_pending(home) == []
    assert "only for Jul'26" in context(result) and "memory tool again" in context(result)


def test_unrelated_reply_disarms_and_keeps_the_note_waiting(home, show, monkeypatch, capsys):
    stage_and_show(home, show, "rule")
    prompt_hook(monkeypatch, capsys, "what is the September P&L?")
    assert len(store.list_pending(home)) == 1
    prompt_hook(monkeypatch, capsys, "yes")
    assert store.read_entries(home, "memory") == []


def test_next_item_is_asked_after_a_decision(home, show, monkeypatch, capsys):
    stage_and_show(home, show, "one")
    store.submit(home, "memory", [add("two")])
    result = prompt_hook(monkeypatch, capsys, "yes")
    assert "#2" in context(result)
    question = store.question_for(2, "memory", [add("two")])
    show(f"Sure, here is the plan.\n\n{question}")
    prompt_hook(monkeypatch, capsys, "yes")
    assert store.read_entries(home, "memory") == ["one", "two"]


def test_skills_are_never_approved_by_a_chat_reply(home, show, tmp_path, monkeypatch, capsys):
    from hermes_memory import skills
    monkeypatch.setenv("HERMES_MEMORY_SKILLS_DIR", str(tmp_path / "skills"))
    res = skills.submit(home, {"action": "create", "name": "monthly-close", "description": "d", "content": "steps"})
    assert res["ask_user"] is False and "/memory-pending" in res["message"]
    show("Skill #1 is waiting. Reply yes, no, or tell me what to change.")
    prompt_hook(monkeypatch, capsys, "yes")
    assert not (tmp_path / "skills" / "monthly-close").exists()


# ---------- learning nudge ----------

def test_nudge_every_n_prompts(home, monkeypatch, capsys):
    store.set_learn_every(home, 3)
    outputs = [context(prompt_hook(monkeypatch, capsys, f"question {i}")) for i in range(6)]
    assert [i for i, c in enumerate(outputs) if "learning check" in c.lower()] == [2, 5]


def test_nudge_counts_per_session(home, monkeypatch, capsys):
    store.set_learn_every(home, 2)
    a = context(prompt_hook(monkeypatch, capsys, "q", session="A"))
    b = context(prompt_hook(monkeypatch, capsys, "q", session="B"))
    assert a == "" and b == ""


def test_nudge_can_be_disabled(home, monkeypatch, capsys):
    store.set_learn_every(home, 0)
    assert all(prompt_hook(monkeypatch, capsys, "q") == {} for _ in range(10))


def test_prompt_hook_survives_garbage_input(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "prompt"]) == 0


# ---------- slash-command arguments ----------

def test_learn_command_show_set_and_validate(home, capsys):
    assert cli.main(["learn", ""]) == 0 and "every 6 prompts" in capsys.readouterr().out
    assert cli.main(["learn", "3"]) == 0 and store.load_config(home)["learn_every"] == 3
    assert cli.main(["learn", "0"]) == 0 and "off" in capsys.readouterr().out.splitlines()[-1]
    assert cli.main(["learn", "lots"]) == 1


def test_approval_command_with_empty_quoted_argument_shows_mode(home, capsys):
    assert cli.main(["approval", ""]) == 0
    assert "approval mode: auto" in capsys.readouterr().out
