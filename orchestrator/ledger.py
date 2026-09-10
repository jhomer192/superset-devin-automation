"""Append-only progress ledger stored as issue comments.

GitHub issue bodies have no conditional write (no ETag/If-Match on PATCH), so a read-modify-write
of the body can lose a concurrent update. Comment creation is append-only and atomic per call, so
each event is one comment carrying a machine-readable marker plus a human-readable summary.

Marker format (one line): ``<!-- sda:{json} -->``
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .github_api import GitHubClient

MARKER_RE = re.compile(r"<!--\s*sda:(\{.*?\})\s*-->", re.DOTALL)


@dataclass
class LedgerEntry:
    event: str
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    data: dict[str, Any] = field(default_factory=dict)

    def marker(self) -> str:
        return f"<!-- sda:{json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))} -->"


def parse_entries(comments: list[dict[str, Any]]) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    for comment in comments:
        for match in MARKER_RE.finditer(comment.get("body") or ""):
            try:
                raw = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or "event" not in raw:
                continue
            entries.append(
                LedgerEntry(
                    event=str(raw["event"]),
                    at=str(raw.get("at", "")),
                    data=dict(raw.get("data") or {}),
                )
            )
    return entries


def render_comment(title: str, entry: LedgerEntry, lines: list[str]) -> str:
    # Markdown table rows are passed through: a bullet in front of them stops GitHub rendering
    # the table.
    body = [f"**{title}**", ""]
    body.extend(line if line.startswith("|") else f"- {line}" for line in lines)
    body.extend(["", entry.marker()])
    return "\n".join(body)


class IssueLedger:
    def __init__(self, gh: GitHubClient, repo: str) -> None:
        self._gh = gh
        self._repo = repo
        # A thread is read several times per run (dedupe, regression guard, lineage). One ledger
        # is built per run, so this cache lives exactly as long as the run and never goes stale
        # against another writer.
        self._entries: dict[int, list[LedgerEntry]] = {}

    def read(self, issue_number: int) -> list[LedgerEntry]:
        if issue_number not in self._entries:
            self._entries[issue_number] = parse_entries(
                self._gh.list_issue_comments(self._repo, issue_number)
            )
        return self._entries[issue_number]

    def append(self, issue_number: int, title: str, entry: LedgerEntry, lines: list[str]) -> LedgerEntry:
        self._gh.create_issue_comment(self._repo, issue_number, render_comment(title, entry, lines))
        if issue_number in self._entries:
            self._entries[issue_number].append(entry)
        return entry


def find(entries: list[LedgerEntry], event: str, **where: Any) -> list[LedgerEntry]:
    return [e for e in entries if e.event == event and all(e.data.get(k) == v for k, v in where.items())]
