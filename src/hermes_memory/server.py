"""MCP server exposing the `memory` tool to Claude.

Deliberately exposes NO approve/reject tool: approval must come from the user's own command,
so the model can never promote its own guess into a rule.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from . import store

app = MCPServer(
    "hermes-memory",
    instructions="Persistent memory shared across all of the user's projects. "
                 "Use the `memory` tool to add, patch or remove notes.",
)


@app.tool()
def memory(target: str, operations: list[dict]) -> str:
    """Save, edit or delete durable notes that load into every future session, in every project.

    target: "user" (who the user is, preferences, communication style) or
            "memory" (lessons about their work: environment quirks, conventions, decisions).
    operations: list of {"action": "add", "content": ...} |
                {"action": "patch", "old_text": <unique span>, "content": <replacement span>} |
                {"action": "remove", "old_text": <unique substring of the entry>}.
    All operations in one call share the target; the size limit is checked on the final result,
    so free space and add in the same batch when a store is full.
    A result with applied=false means the write is waiting for the user's approval: tell them what
    you want to save and ask them to confirm. Never say it is saved.
    """
    return json.dumps(store.submit(store.default_home(), target, operations), ensure_ascii=False)


def registered_tool_names() -> list[str]:
    import asyncio

    return [t.name for t in asyncio.run(app.list_tools())]


def main() -> None:
    app.run("stdio")


if __name__ == "__main__":
    main()
