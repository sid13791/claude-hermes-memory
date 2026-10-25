---
description: Approve a waiting memory so it is saved to the notebook
argument-hint: <id|all>
disable-model-invocation: true
allowed-tools: Bash(uv run --quiet --no-dev --directory:*)
---

!`uv run --quiet --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory approve $ARGUMENTS`

Tell the user the result above in one line. Approved notes load from the next session on.
