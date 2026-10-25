---
description: Show the shared memory notebook (USER.md + MEMORY.md) and how full it is
allowed-tools: Bash(uv run --quiet --no-dev --directory:*)
---

!`uv run --quiet --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory show`

Show the output above to the user as-is, in a code block. Do not call the memory tool.
