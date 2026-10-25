---
description: Show or set how often Claude is reminded to save what it learned (every N prompts; 0 = off)
argument-hint: [number]
disable-model-invocation: true
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory learn '$ARGUMENTS'`

Tell the user the result above in one line.
