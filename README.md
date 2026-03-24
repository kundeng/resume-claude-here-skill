# resume-claude-here

`resume-claude-here` is a Codex/Claude-compatible skill for recovering a prior Claude Code CLI session from natural-language hints, searching Claude history by topic/date/project, and importing the useful context into the current conversation.

It is designed for the common handoff case where a Claude session hit a limit and you want to continue in another agent without manually digging through transcript files.

Search terms this skill is meant to match include:

- continue a Claude session here
- resume previous Claude chat
- import old Claude transcript
- find that Claude conversation from earlier
- continue after Claude hit the limit

## Install

From GitHub via the Skills CLI:

```bash
npx skills add https://github.com/kundeng/resume-claude-here --skill resume-claude-here
```

## What It Does

- Searches local Claude session persistence under `~/.claude`
- Ranks sessions using natural-language hints, path similarity, branch metadata, and recency
- Exports a focused transcript slice instead of forcing a full raw resume
- Prefers importing useful context into the current agent rather than reopening Claude by default

## Repository Layout

- `SKILL.md`: skill instructions and workflow
- `scripts/claude_session_tool.py`: session search/export helper
- `references/research-notes.md`: notes on Claude CLI and session persistence behavior
- `evals/trigger-evals.json`: starter trigger evals

## Local Usage

Search for likely sessions:

```bash
python3 scripts/claude_session_tool.py search \
  --query "that pdfwiki desktop research from sunday" \
  --cwd "$PWD" \
  --limit 8
```

Export a focused transcript slice:

```bash
python3 scripts/claude_session_tool.py export \
  --session <session-id> \
  --tail 12 \
  --format markdown
```

## Requirements

- Python 3
- Local Claude Code session data under `~/.claude`

No third-party Python dependencies are required.

## Notes

- This skill searches the machine where it is running. If you are using VS Code Remote SSH, it will only see Claude sessions stored on the remote host unless you sync or mount your local `~/.claude` data there.
- The skill can reopen Claude with `claude --continue` or `claude --resume`, but only when explicitly requested.

## License

MIT
