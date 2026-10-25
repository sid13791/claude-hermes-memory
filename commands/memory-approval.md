---
description: Show or set approval mode (off = save freely, auto = work rules wait, on = everything waits)
argument-hint: [off|auto|on]
disable-model-invocation: true
allowed-tools: Bash(uv run --quiet --no-dev --directory:*)
---

!`uv run --quiet --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory approval $ARGUMENTS`

Tell the user the result above in one line.
