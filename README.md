# hermes-memory

Persistent memory for Claude Code, modelled on the memory system of
[Hermes Agent](https://github.com/NousResearch/hermes-agent). This is an independent project; it is
not affiliated with Nous Research.

Claude Code keeps memory per project folder, so a preference learned in one folder is invisible in the
next. This plugin adds a **shared notebook** that loads into every session in every project, next to
Claude Code's own per-project memory (which keeps working as before).

## How it works

| Piece | What it does |
|---|---|
| `USER.md` | Notes about you: preferences, communication style. Cap 1,375 chars. |
| `MEMORY.md` | Lessons about your work that apply across projects. Cap 2,200 chars. |
| SessionStart hook | Injects both files into the session as a frozen snapshot, with a usage header (`[67% — 1,474/2,200 chars]`). Writes made during a session show up in the next one. |
| `memory` MCP tool | Claude adds, patches or removes notes. Over the cap → refused; Claude must merge or delete first. |
| Security scan | Blocks prompt-injection phrases, API keys / passwords, exfiltration commands and invisible unicode. |
| Approval queue | Notes can wait for your yes/no before they are saved (see below). |

Files live in `~/.claude/hermes-memory/` (override with `HERMES_MEMORY_HOME`).

### Approval modes

| Mode | `user` notes | `memory` notes |
|---|---|---|
| `off` | saved immediately | saved immediately |
| `auto` (default) | saved immediately, unless they read like a rule (always / never / must) or contain numbers or amounts | wait for approval |
| `on` | wait for approval | wait for approval |

The plugin gives Claude **no tool to approve its own notes**: the `memory` tool can only stage them, and
the approve / reject / approval commands are marked `disable-model-invocation`, so only you can run them.

**Limit:** Claude Code still gives Claude general Bash and file-editing tools. In principle it could run
the CLI or edit `~/.claude/hermes-memory/` directly. To close that gap, add deny rules to your
`~/.claude/settings.json`:

```json
{
  "permissions": {
    "deny": [
      "Edit(~/.claude/hermes-memory/**)",
      "Write(~/.claude/hermes-memory/**)",
      "Bash(*hermes-memory approve*)",
      "Bash(*hermes-memory reject*)",
      "Bash(*hermes-memory approval*)"
    ]
  }
}
```

A later release will add in-chat yes/no confirmation that reads your typed reply through a prompt hook.

## Commands

| Command | What it does |
|---|---|
| `/memory` | Show both notebooks and how full they are |
| `/memory-pending` | List notes waiting for approval |
| `/memory-approve <id\|all>` | Save a waiting note |
| `/memory-reject <id\|all>` | Throw a waiting note away |
| `/memory-approval [off\|auto\|on]` | Show or change the approval mode |

## Install

Requires [uv](https://docs.astral.sh/uv/) on your PATH.

```
/plugin marketplace add sid13791/claude-hermes-memory
/plugin install hermes-memory@claude-hermes-memory
```

Restart Claude Code. The first session can take up to a minute while uv creates the environment and
installs the MCP SDK; later sessions start in well under a second. If the notebook does not appear in the
first session, start a new one.

## Develop

```bash
uv sync
uv run pytest --cov=hermes_memory
```

## Roadmap

1. **Shared notebook**: this release.
2. Chat archive with full-text search (`recall`), saved before Claude Code's 30-day transcript cleanup.
3. Learning loop: save reminders, flush before compaction, in-chat yes/no confirmation via a prompt hook.
4. `/memory-import`: merge existing per-project memory folders, flagging duplicates and contradictions.
5. Optional Google OKF (Open Knowledge Format) export and lookup.

## License

MIT
