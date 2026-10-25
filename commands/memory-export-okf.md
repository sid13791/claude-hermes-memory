---
description: Export your shared notebook and skills as an OKF v0.2 bundle (an empty folder you choose)
argument-hint: "<full path to an empty folder>  (no apostrophes in the path)"
disable-model-invocation: true
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory okf-export '$ARGUMENTS'`

Tell the user the result above in one line. Remind them the export contains their personal notes, so they should
check it before sharing or pushing it anywhere. The chat archive is never exported.
