#!/usr/bin/env python3
"""Search and export Claude Code sessions from local persistence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable


STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "bring",
    "chat",
    "claude",
    "code",
    "continue",
    "conversation",
    "for",
    "from",
    "here",
    "i",
    "in",
    "into",
    "it",
    "last",
    "my",
    "new",
    "of",
    "old",
    "on",
    "one",
    "open",
    "previous",
    "prior",
    "project",
    "pull",
    "resume",
    "session",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
    "work",
}

# Higher number = preferred when two files contain the same session.
SOURCE_PRIORITY = {"explicit": 4, "project": 3, "transcript": 2, "subagent": 1}
DEFAULT_RESULT_LIMIT = 8
RECENT_USER_TEXT_LIMIT = 3
EXACT_MATCH_SCORE = 80.0
TOKEN_MATCH_SCORE = 12.0
TOKEN_MATCH_CAP = 48.0
FUZZY_MATCH_MULTIPLIER = 35.0
SAME_CWD_SCORE = 50.0
SAME_REPO_SCORE = 25.0
SAME_BASENAME_SCORE = 10.0

# Type alias so function signatures read more clearly.
JsonObject = dict[str, object]


@dataclass
class MessageEntry:
    """One user-visible message extracted from a Claude transcript."""

    role: str
    timestamp: str
    text: str
    kind: str = "message"


@dataclass
class SessionSummary:
    """Normalized metadata and extracted content for one Claude session."""

    session_id: str
    source_file: Path
    source_kind: str
    title: str | None = None
    slug: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    last_timestamp: str | None = None
    first_timestamp: str | None = None
    first_user_text: str | None = None
    recent_user_texts: list[str] = field(default_factory=list)
    message_count: int = 0
    user_count: int = 0
    assistant_count: int = 0
    entries: list[MessageEntry] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0


def parse_args() -> argparse.Namespace:
    """Build and parse the command-line interface."""

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser(
        "search",
        help="Search and rank sessions",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    search.add_argument("--query", required=True, help="Natural-language session hint")
    search.add_argument("--cwd", help="Current working directory for rank boosting")
    search.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RESULT_LIMIT,
        help="Maximum results to return",
    )
    search.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text shortlist",
    )

    export = subparsers.add_parser(
        "export",
        help="Export a session transcript",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    export.add_argument(
        "--session",
        required=True,
        help="Session ID or path to a session jsonl",
    )
    export.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Export format",
    )
    export.add_argument(
        "--tail",
        type=int,
        default=0,
        help="Export only the last N message entries; 0 means all",
    )
    export.add_argument(
        "--include-tool-results",
        action="store_true",
        help="Include tool result payloads when available",
    )
    export.add_argument("--output", help="Write export to a file instead of stdout")

    return parser.parse_args()


def discover_session_files() -> list[tuple[Path, str]]:
    """Return candidate session files with their inferred source kinds."""

    home = Path.home()
    files: list[tuple[Path, str]] = []

    projects_root = home / ".claude" / "projects"
    if projects_root.exists():
        for path in projects_root.rglob("*.jsonl"):
            kind = "subagent" if "subagents" in path.parts else "project"
            files.append((path, kind))

    transcripts_root = home / ".claude" / "transcripts"
    if transcripts_root.exists():
        for path in transcripts_root.glob("*.jsonl"):
            files.append((path, "transcript"))

    return files


def load_json_lines(path: Path) -> Iterable[JsonObject]:
    """Yield decoded JSON objects from a jsonl file, skipping invalid lines."""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_text(content: object, include_tool_results: bool = False) -> str:
    """Normalize Claude message content into plain text."""

    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = str(item.get("text", "")).strip()
            if text:
                parts.append(text)
        elif item_type == "tool_result" and include_tool_results:
            text = str(item.get("content", "")).strip()
            if text:
                parts.append(f"[tool_result]\n{text}")
    return "\n".join(parts).strip()


def summarize_session(
    path: Path,
    kind: str,
    include_tool_results: bool = False,
) -> SessionSummary | None:
    """Summarize one session file into normalized metadata and message entries."""

    summary: SessionSummary | None = None
    cwd_counter: Counter[str] = Counter()
    user_texts: list[str] = []

    for record in load_json_lines(path):
        # Claude stores session IDs under different keys depending on file format.
        session_id = (
            record.get("sessionId")
            or record.get("session_id")
            or path.stem
        )
        if summary is None:
            summary = SessionSummary(
                session_id=str(session_id),
                source_file=path,
                source_kind=kind,
            )

        timestamp = str(record.get("timestamp", ""))
        if timestamp:
            if not summary.first_timestamp or timestamp < summary.first_timestamp:
                summary.first_timestamp = timestamp
            if not summary.last_timestamp or timestamp > summary.last_timestamp:
                summary.last_timestamp = timestamp

        if record.get("type") == "ai-title":
            summary.title = str(record.get("aiTitle") or "").strip() or summary.title

        if record.get("slug"):
            summary.slug = str(record.get("slug"))

        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            cwd_counter[cwd] += 1

        git_branch = record.get("gitBranch")
        if isinstance(git_branch, str) and git_branch:
            summary.git_branch = git_branch

        role = None
        message = record.get("message")
        if isinstance(message, dict):
            role = message.get("role")
            text = extract_text(
                message.get("content"),
                include_tool_results=include_tool_results,
            )
        else:
            role = record.get("type")
            text = ""

        if role in ("user", "assistant") and text:
            summary.message_count += 1
            if role == "user":
                summary.user_count += 1
                user_texts.append(text)
                if not summary.first_user_text and not record.get("isMeta"):
                    summary.first_user_text = text
            elif role == "assistant":
                summary.assistant_count += 1
            summary.entries.append(
                MessageEntry(role=role, timestamp=timestamp, text=text)
            )

    if summary is None:
        return None

    # Pick whichever cwd appeared most often across all records.
    if cwd_counter:
        summary.cwd = cwd_counter.most_common(1)[0][0]

    summary.recent_user_texts = user_texts[-RECENT_USER_TEXT_LIMIT:]
    return summary


def tokenize(text: str) -> list[str]:
    """Split text into lowercase searchable tokens with domain stop words removed."""

    return [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9._/-]+", text.lower())
        if token not in STOP_WORDS and len(token) > 1
    ]


def basename_hint(path_str: str | None) -> str:
    """Return the last path component when available."""

    if not path_str:
        return ""
    return Path(path_str.rstrip("/")).name


def session_search_blob(session: SessionSummary) -> str:
    """Build the text blob used for ranking a session against a query."""

    fields = [
        session.title or "",
        session.slug or "",
        session.cwd or "",
        basename_hint(session.cwd),
        session.git_branch or "",
        session.first_user_text or "",
        " ".join(session.recent_user_texts),
    ]
    return "\n".join(x for x in fields if x).lower()


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse ISO-like timestamps used in Claude transcript files."""

    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def recency_score(timestamp: str | None) -> float:
    """Return a freshness bonus based on how recently a session was active."""

    dt = parse_timestamp(timestamp)
    if dt is None:
        return 0.0
    age_hours = max(
        (
            datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        ).total_seconds()
        / 3600,
        0.0,
    )
    # Stepped bonus: recent sessions get a bigger boost.
    if age_hours <= 24:
        return 20.0
    if age_hours <= 24 * 7:
        return 12.0
    if age_hours <= 24 * 30:
        return 6.0
    return 0.0


def score_session(session: SessionSummary, query: str, cwd: str | None) -> float:
    """Score one session against the user's query and current working directory."""

    blob = session_search_blob(session)
    query_lower = query.lower().strip()
    query_tokens = tokenize(query)
    blob_tokens = set(tokenize(blob))

    score = 0.0
    reasons: list[str] = []

    if query_lower and query_lower in blob:
        score += EXACT_MATCH_SCORE
        reasons.append("exact phrase match")

    overlap = [token for token in query_tokens if token in blob_tokens]
    if overlap:
        overlap_score = min(TOKEN_MATCH_SCORE * len(overlap), TOKEN_MATCH_CAP)
        score += overlap_score
        reasons.append("token match: " + ", ".join(overlap[:4]))

    # ratio() returns 0.0–1.0 measuring overall similarity between two strings.
    seq = SequenceMatcher(None, query_lower, blob).ratio()
    if seq >= 0.2:
        fuzzy = round(seq * FUZZY_MATCH_MULTIPLIER, 1)
        score += fuzzy
        reasons.append(f"fuzzy similarity {seq:.2f}")

    if cwd and session.cwd:
        cwd_path = Path(cwd).expanduser().resolve()
        session_path = Path(session.cwd).expanduser()
        try:
            session_resolved = session_path.resolve()
        except FileNotFoundError:
            session_resolved = session_path

        if str(session_resolved) == str(cwd_path):
            score += SAME_CWD_SCORE
            reasons.append("same cwd")
        elif str(session_resolved).startswith(
            str(cwd_path)
        ) or str(cwd_path).startswith(str(session_resolved)):
            score += SAME_REPO_SCORE
            reasons.append("same repo path")
        elif basename_hint(str(session_resolved)) == basename_hint(str(cwd_path)):
            score += SAME_BASENAME_SCORE
            reasons.append("same cwd basename")

    recency = recency_score(session.last_timestamp)
    score += recency
    if recency > 0:
        reasons.append("recent activity")

    session.score = round(score, 1)
    session.reasons = reasons
    return session.score


def should_replace_summary(
    current: SessionSummary,
    challenger: SessionSummary,
) -> bool:
    """Decide whether one candidate summary should replace another."""

    current_priority = SOURCE_PRIORITY.get(current.source_kind, 0)
    challenger_priority = SOURCE_PRIORITY.get(challenger.source_kind, 0)
    # Tiebreak order: higher score > higher source priority > more entries.
    return (
        challenger.score > current.score
        or (
            challenger.score == current.score
            and challenger_priority > current_priority
        )
        or (
            challenger.score == current.score
            and challenger_priority == current_priority
            and len(challenger.entries) > len(current.entries)
        )
    )


def search_sessions(query: str, cwd: str | None, limit: int) -> list[SessionSummary]:
    """Search, score, deduplicate, and rank sessions."""

    best_by_session: dict[str, SessionSummary] = {}
    for path, kind in discover_session_files():
        session = summarize_session(path, kind)
        if session is None:
            continue
        score_session(session, query, cwd)
        if session.score > 0:
            existing = best_by_session.get(session.session_id)
            if existing is None:
                best_by_session[session.session_id] = session
                continue
            if should_replace_summary(existing, session):
                best_by_session[session.session_id] = session

    sessions = list(best_by_session.values())
    sessions.sort(key=lambda item: (item.score, item.last_timestamp or ""), reverse=True)
    return sessions[:limit]


def text_preview(session: SessionSummary) -> str:
    """Render one ranked session as a compact terminal-friendly line."""

    title = session.title or session.slug or basename_hint(session.cwd) or session.session_id
    when = session.last_timestamp or "unknown time"
    cwd = basename_hint(session.cwd) or session.cwd or "unknown cwd"
    reasons = "; ".join(session.reasons[:3]) if session.reasons else "metadata match"
    return (
        f"{title} | {when} | cwd={cwd} | score={session.score:.1f} | "
        f"session={session.session_id} | {reasons}"
    )


def session_lookup(
    identifier: str,
    include_tool_results: bool = False,
) -> SessionSummary:
    """Resolve a session by explicit path, session ID, title, or slug."""

    path = Path(identifier).expanduser()
    if path.exists():
        session = summarize_session(
            path,
            "explicit",
            include_tool_results=include_tool_results,
        )
        if session is None:
            raise SystemExit(f"Could not parse session file: {path}")
        return session

    for candidate_path, kind in discover_session_files():
        session = summarize_session(
            candidate_path,
            kind,
            include_tool_results=include_tool_results,
        )
        if session is None:
            continue
        title = session.title or ""
        if identifier in {session.session_id, title, session.slug or ""}:
            return session

    raise SystemExit(f"Session not found: {identifier}")


def selected_entries(session: SessionSummary, tail: int) -> list[MessageEntry]:
    """Return the entry slice requested by the caller."""

    if tail > 0:
        return session.entries[-tail:]
    return session.entries


def export_markdown(session: SessionSummary, tail: int) -> str:
    """Render a session as Markdown."""

    entries = selected_entries(session, tail)
    title = session.title or session.slug or session.session_id
    lines = [
        f"# Claude Session Export: {title}",
        "",
        f"- Session ID: `{session.session_id}`",
        f"- Source: `{session.source_file}`",
        f"- Source kind: `{session.source_kind}`",
        f"- CWD: `{session.cwd or 'unknown'}`",
        f"- Git branch: `{session.git_branch or 'unknown'}`",
        f"- First activity: `{session.first_timestamp or 'unknown'}`",
        f"- Last activity: `{session.last_timestamp or 'unknown'}`",
        f"- Messages exported: `{len(entries)}`",
        "",
    ]

    for entry in entries:
        lines.append(f"## {entry.role.title()} | {entry.timestamp or 'unknown time'}")
        lines.append("")
        lines.append(entry.text.strip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def export_json(session: SessionSummary, tail: int) -> str:
    """Render a session as JSON."""

    entries = selected_entries(session, tail)
    payload = {
        "session_id": session.session_id,
        "title": session.title,
        "slug": session.slug,
        "cwd": session.cwd,
        "git_branch": session.git_branch,
        "first_timestamp": session.first_timestamp,
        "last_timestamp": session.last_timestamp,
        "message_count": len(entries),
        "entries": [
            {
                "role": entry.role,
                "timestamp": entry.timestamp,
                "text": entry.text,
            }
            for entry in entries
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def write_output(content: str, output: str | None) -> None:
    """Write content to stdout or to a file path."""

    if output:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(str(out_path))
        return
    sys.stdout.write(content)


def main() -> None:
    """Dispatch CLI commands."""

    args = parse_args()
    if args.command == "search":
        results = search_sessions(args.query, args.cwd, args.limit)
        if args.json:
            payload = [
                {
                    "session_id": item.session_id,
                    "title": item.title,
                    "slug": item.slug,
                    "cwd": item.cwd,
                    "git_branch": item.git_branch,
                    "last_timestamp": item.last_timestamp,
                    "score": item.score,
                    "reasons": item.reasons,
                    "source_file": str(item.source_file),
                    "source_kind": item.source_kind,
                }
                for item in results
            ]
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            return

        for index, item in enumerate(results, start=1):
            sys.stdout.write(f"{index}. {text_preview(item)}\n")
        return

    if args.command == "export":
        session = session_lookup(
            args.session,
            include_tool_results=args.include_tool_results,
        )
        if args.format == "markdown":
            content = export_markdown(session, args.tail)
        else:
            content = export_json(session, args.tail)
        write_output(content, args.output)
        return


if __name__ == "__main__":
    main()
