---
description: Show recent memory activity (saved, waiting, approved, rejected, blocked) with project and reason
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory log 30`

Show the log above to the user as-is, in a code block. Then, in one or two plain sentences, point out anything that
looks wrong: a note that was saved but sounds like a guess, or something blocked that the user may have wanted.
