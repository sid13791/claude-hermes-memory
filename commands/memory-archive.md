---
description: Copy all existing Claude Code chats into the searchable archive (safe to re-run)
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory archive`

Tell the user the result above in one or two plain sentences.
