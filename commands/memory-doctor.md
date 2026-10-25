---
description: Health check for the shared memory (folder, config, size limits, approval queue, MCP SDK)
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory doctor`

Show the report above to the user as-is, in a code block. Also say whether the `memory` tool is available to you in
this chat (check your own tool list for a tool ending in `__memory` from hermes-memory). For every FAIL line, give
the one concrete fix in plain language.
