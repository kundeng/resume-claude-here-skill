# Research Notes

These notes capture the concrete Claude Code behaviors this skill relies on.

## Verified CLI behavior

Local machine observations on 2026-03-19:

- `claude --help` exposes:
  - `--continue`
  - `--resume [value]`
  - `--fork-session`
  - `--from-pr`
  - `--output-format text|json|stream-json`
  - `--no-session-persistence`
- `claude -p --help` shows the same resume flags in print mode.
- `--output-format=stream-json` requires `--verbose`.
- `--print --resume ... --output-format json` returns a structured result object and surfaces failures with `is_error: true`.

## Official docs confirmed

From Claude Code docs:

- `claude --continue` continues the most recent conversation in the current directory.
- `claude --resume` opens a picker or resumes by name.
- `claude --from-pr <number>` resumes a PR-linked session.
- `/resume` can switch sessions from inside an active session.
- The picker shows metadata such as session name, elapsed time, message count, and git branch.
- Sessions are stored per project directory, and the picker can search/filter them.
- `claude --continue --print "prompt"` is a supported non-interactive pattern.

## Local persistence format observed

The installed CLI persists sessions locally as JSONL message logs. Practical fields observed in saved records include:

- `sessionId`
- `timestamp`
- `cwd`
- `gitBranch`
- `entrypoint`
- `slug`
- `type`
- `message`

Also observed:

- project-scoped session trees with per-session JSONL files
- standalone transcript JSONL files
- subagent JSONL files nested under a session directory
- `ai-title` records that provide a usable session title

The helper script in `scripts/claude_session_tool.py` already knows how to inspect the local persistence layout. Prefer the script over hardcoding storage assumptions in ad hoc commands.

## Practical implications

- For a true continuation, use the CLI resume flags.
- For importing old context into the current thread, export transcript slices instead of pretending the active thread has magically inherited old state.
- Use current working directory as a ranking signal, not as an absolute filter.
- Prefer explicit session IDs over fuzzy names when executing a resume command.
- If the user arrives because Claude hit a usage or rate limit, importing the old session into the current agent should be the default.
- Treat actual Claude reopen as an explicit edge case, not the normal workflow.
