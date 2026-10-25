import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated memory home so tests never touch ~/.claude/hermes-memory."""
    monkeypatch.setenv("HERMES_MEMORY_HOME", str(tmp_path))
    # Never index the developer's real Claude Code transcripts during tests.
    empty = tmp_path / "no-transcripts"
    empty.mkdir()
    monkeypatch.setenv("HERMES_MEMORY_TRANSCRIPTS", str(empty))
    return tmp_path
