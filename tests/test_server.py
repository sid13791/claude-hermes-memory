import json

from hermes_memory import server, store


def test_memory_tool_writes_through_store(home):
    out = json.loads(server.memory("user", [{"action": "add", "content": "Likes tables"}]))
    assert out["success"] and out["applied"]
    assert store.read_entries(home, "user") == ["Likes tables"]


def test_server_exposes_no_approval_tool():
    # Approval must come from the user's own command, never from the model.
    names = {t for t in dir(server) if "approve" in t.lower() or "reject" in t.lower()}
    assert names == set()


def test_server_registers_only_the_memory_tool():
    assert server.registered_tool_names() == ["memory"]
