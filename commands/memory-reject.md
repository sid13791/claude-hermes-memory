---
description: Throw away a waiting memory
argument-hint: <id|all>
disable-model-invocation: true
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory reject '$ARGUMENTS'`

Tell the user the result above in one line.
