"""Phase 2: chat archive (SQLite FTS5) and recall."""

import io
import json

import pytest

from hermes_memory import archive, cli, server


def line(kind, text_or_content, *, session="s1", uuid, ts="2026-10-01T10:00:00Z", cwd=r"C:\work\Yulu Bot",
         sidechain=False):
    content = text_or_content
    return json.dumps({"type": kind, "sessionId": session, "uuid": uuid, "timestamp": ts, "cwd": cwd,
                       "isSidechain": sidechain, "message": {"role": kind, "content": content}})


@pytest.fixture
def transcripts(home, tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "proj").mkdir(parents=True)
    monkeypatch.setenv("HERMES_MEMORY_TRANSCRIPTS", str(root))
    return root


def write(path, *lines):
    with path.open("a", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")


@pytest.fixture
def chat(transcripts):
    path = transcripts / "proj" / "s1.jsonl"
    write(path,
          json.dumps({"type": "queue-operation", "operation": "enqueue"}),
          line("user", "Should we use Sonnet for the Yulu agent?", uuid="u1"),
          line("assistant", [{"type": "thinking", "thinking": "secret thoughts"},
                             {"type": "text", "text": "Sonnet is too expensive; keep Gemini Flash."},
                             {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}], uuid="a1"),
          line("user", [{"type": "tool_result", "content": "file listing noise"}], uuid="u2"),
          line("user", "<system-reminder>internal note</system-reminder>ok, keep Gemini", uuid="u3"),
          line("assistant", "Subagent chatter", uuid="a2", sidechain=True))
    return path


def test_indexes_user_and_assistant_text_only(home, chat):
    archive.index_file(home, chat)
    texts = [m["text"] for m in archive.session_messages(home, "s1")]
    assert texts == ["Should we use Sonnet for the Yulu agent?",
                     "Sonnet is too expensive; keep Gemini Flash.",
                     "ok, keep Gemini"]


def test_search_finds_the_decision_with_project_and_date(home, chat):
    archive.index_file(home, chat)
    hits = archive.search(home, "sonnet expensive")
    assert hits[0]["session_id"] == "s1"
    assert hits[0]["project"] == "Yulu Bot"
    assert hits[0]["ts"].startswith("2026-10-01")
    assert "expensive" in hits[0]["snippet"]


def test_search_falls_back_to_any_word_when_all_words_miss(home, chat):
    archive.index_file(home, chat)
    assert archive.search(home, "sonnet zebra")


def test_search_query_with_fts_syntax_does_not_crash(home, chat):
    archive.index_file(home, chat)
    for q in ['"unbalanced', "C++ AND (", "NEAR(", "*", "-x"]:
        archive.search(home, q)  # must not raise


def test_empty_query_returns_nothing(home, chat):
    archive.index_file(home, chat)
    assert archive.search(home, "   ") == []


def test_reindex_is_incremental(home, chat):
    archive.index_file(home, chat)
    write(chat, line("user", "new question about pricing", uuid="u9"))
    added = archive.index_file(home, chat)
    assert added == 1
    assert archive.index_file(home, chat) == 0
    assert archive.search(home, "pricing")


def test_partial_last_line_is_left_for_next_time(home, chat):
    archive.index_file(home, chat)
    full = line("user", "half written message", uuid="u10")
    with chat.open("a", encoding="utf-8") as fh:
        fh.write(full[:20])
    assert archive.index_file(home, chat) == 0
    with chat.open("a", encoding="utf-8") as fh:
        fh.write(full[20:] + "\n")
    assert archive.index_file(home, chat) == 1


def test_duplicate_uuid_is_not_indexed_twice(home, chat, transcripts):
    archive.index_file(home, chat)
    copy = transcripts / "proj" / "copy.jsonl"
    copy.write_text(chat.read_text(encoding="utf-8"), encoding="utf-8")
    assert archive.index_file(home, copy) == 0


def test_secrets_are_redacted_before_storage(home, transcripts):
    path = transcripts / "proj" / "s2.jsonl"
    key = "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz123456"
    write(path, line("user", f"my key is {key}", session="s2", uuid="k1"))
    archive.index_file(home, path)
    text = archive.session_messages(home, "s2")[0]["text"]
    assert key not in text and "[REDACTED]" in text


def test_scroll_returns_neighbours_in_order(home, chat):
    archive.index_file(home, chat)
    first = archive.session_messages(home, "s1")[0]
    around = archive.scroll(home, "s1", first["id"], window=1)
    assert [m["text"] for m in around][:2] == ["Should we use Sonnet for the Yulu agent?",
                                              "Sonnet is too expensive; keep Gemini Flash."]


def test_project_filter(home, chat, transcripts):
    other = transcripts / "proj" / "s3.jsonl"
    write(other, line("user", "Sonnet in the Belgian project", session="s3", uuid="b1", cwd=r"C:\work\Belgian"))
    archive.index_file(home, chat)
    archive.index_file(home, other)
    hits = archive.search(home, "sonnet", project="belgian")
    assert {h["session_id"] for h in hits} == {"s3"}


def test_sweep_indexes_every_transcript_and_reports_stats(home, chat, transcripts):
    write(transcripts / "proj" / "s4.jsonl", line("user", "another chat", session="s4", uuid="x1"))
    stats = archive.sweep(home)
    assert stats["files"] == 2 and stats["messages"] == 4
    assert archive.stats(home)["sessions"] == 2


def test_sweep_respects_time_budget(home, chat):
    stats = archive.sweep(home, budget_seconds=0)
    assert stats["files"] == 0 and stats["skipped_for_time"] >= 1


def test_corrupt_jsonl_lines_are_skipped(home, transcripts):
    path = transcripts / "proj" / "s5.jsonl"
    write(path, "{not json", line("user", "survivor", session="s5", uuid="z1"))
    assert archive.index_file(home, path) == 1


# ---------- hooks, CLI, MCP ----------

def test_stop_hook_indexes_the_current_transcript(home, chat, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(chat)})))
    assert cli.main(["hook", "stop"]) == 0
    assert archive.search(home, "gemini")


def test_stop_hook_tolerates_missing_transcript(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": "C:/nope.jsonl"})))
    assert cli.main(["hook", "stop"]) == 0


def test_recall_cli_prints_hits(home, chat, capsys):
    archive.index_file(home, chat)
    assert cli.main(["recall", "sonnet", "expensive"]) == 0
    out = capsys.readouterr().out
    assert "Yulu Bot" in out and "expensive" in out


def test_archive_cli_backfills(home, chat, capsys):
    assert cli.main(["archive"]) == 0
    assert "sessions: 1" in capsys.readouterr().out


def test_recall_tool_search_and_scroll(home, chat):
    out = json.loads(server.recall(query="sonnet expensive"))
    assert out["results"][0]["project"] == "Yulu Bot"
    hit = out["results"][0]
    scrolled = json.loads(server.recall(session_id=hit["session_id"], around_message_id=hit["message_id"]))
    assert any("Sonnet" in m["text"] for m in scrolled["messages"])


def test_recall_tool_needs_query_or_session(home):
    out = json.loads(server.recall())
    assert "error" in out


def test_server_registers_memory_and_recall():
    assert sorted(server.registered_tool_names()) == ["memory", "recall"]
