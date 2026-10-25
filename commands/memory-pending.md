---
description: List memories waiting for your approval
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory pending`

Show the list above to the user as-is, then remind them: `/memory-approve <id|all>` or `/memory-reject <id|all>`.
Do not approve or reject anything yourself.
