---
description: List, add or remove the OKF (Open Knowledge Format) bundles Claude can look up
argument-hint: "[add <full folder path> | remove <full folder path>]  (no apostrophes in the path)"
disable-model-invocation: true
allowed-tools: Bash(uv run --quiet --frozen --no-dev --directory:*)
---

!`uv run --quiet --frozen --no-dev --directory "${CLAUDE_PLUGIN_ROOT}" hermes-memory okf '$ARGUMENTS'`

Tell the user the result above in one or two plain sentences. If a bundle was added, mention that Claude will now
check it (with the okf_lookup tool) before answering company-fact questions, starting in the next chat.
