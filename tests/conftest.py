import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated memory home so tests never touch ~/.claude/hermes-memory."""
    monkeypatch.setenv("HERMES_MEMORY_HOME", str(tmp_path))
    return tmp_path
