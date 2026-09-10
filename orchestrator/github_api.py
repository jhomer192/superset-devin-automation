"""GitHub REST v3 client covering only what the loop needs: issues, comments, PRs, commits."""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from .http import request_json

log = logging.getLogger(__name__)

JSON = dict[str, Any]

CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+(?:https?://github\.com/"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/|(?P<owner2>[\w.-]+)/(?P<repo2>[\w.-]+)#|#)"
    r"(?P<num>\d+)",
    re.IGNORECASE,
)


def closing_issue_numbers(text: str | None, repo: str) -> list[int]:
    """Issue numbers a PR body/title closes via GitHub's closing-keyword syntax."""
    if not text:
        return []
    found: list[int] = []
    for match in CLOSES_RE.finditer(text):
        owner = match.group("owner") or match.group("owner2")
        name = match.group("repo") or match.group("repo2")
        if owner and f"{owner}/{name}".lower() != repo.lower():
            continue
        num = int(match.group("num"))
        if num not in found:
            found.append(num)
    return found


def merged_in_order(pulls: list[JSON]) -> list[JSON]:
    """Keep only merged PRs, ordered by merged_at then number (GitHub sorts closed PRs arbitrarily)."""
    merged = [p for p in pulls if p.get("merged_at")]
    return sorted(merged, key=lambda p: (str(p["merged_at"]), int(p["number"])))


class GitHubClient(Protocol):
    def list_issues(self, repo: str, labels: str, state: str = "open") -> list[JSON]: ...

    def get_issue(self, repo: str, number: int) -> JSON: ...

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> JSON: ...

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None: ...

    def list_issue_comments(self, repo: str, number: int) -> list[JSON]: ...

    def create_issue_comment(self, repo: str, number: int, body: str) -> JSON: ...

    def list_pulls(self, repo: str, state: str = "open") -> list[JSON]: ...

    def get_pull(self, repo: str, number: int) -> JSON: ...

    def list_merged_pulls(self, repo: str, base_branch: str) -> list[JSON]:
        """Every PR merged into base_branch, oldest merge first."""
        ...


class LiveGitHubClient:
    def __init__(self, token: str, api_base: str = "https://api.github.com") -> None:
        self._base = api_base.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return request_json("GET", f"{self._base}{path}", headers=self._headers, params=params)

    def _paged(self, path: str, params: dict[str, Any]) -> list[JSON]:
        out: list[JSON] = []
        page = 1
        while True:
            chunk = self._get(path, {**params, "per_page": 100, "page": page})
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
        return out

    def list_issues(self, repo: str, labels: str, state: str = "open") -> list[JSON]:
        items = self._paged(f"/repos/{repo}/issues", {"labels": labels, "state": state})
        return [i for i in items if "pull_request" not in i]

    def get_issue(self, repo: str, number: int) -> JSON:
        result: JSON = self._get(f"/repos/{repo}/issues/{number}")
        return result

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> JSON:
        result: JSON = request_json(
            "POST",
            f"{self._base}/repos/{repo}/issues",
            headers=self._headers,
            body={"title": title, "body": body, "labels": labels},
        )
        return result

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        request_json(
            "POST",
            f"{self._base}/repos/{repo}/issues/{number}/labels",
            headers=self._headers,
            body={"labels": labels},
        )

    def list_issue_comments(self, repo: str, number: int) -> list[JSON]:
        return self._paged(f"/repos/{repo}/issues/{number}/comments", {})

    def create_issue_comment(self, repo: str, number: int, body: str) -> JSON:
        result: JSON = request_json(
            "POST",
            f"{self._base}/repos/{repo}/issues/{number}/comments",
            headers=self._headers,
            body={"body": body},
        )
        return result

    def list_pulls(self, repo: str, state: str = "open") -> list[JSON]:
        return self._paged(f"/repos/{repo}/pulls", {"state": state})

    def get_pull(self, repo: str, number: int) -> JSON:
        result: JSON = self._get(f"/repos/{repo}/pulls/{number}")
        return result

    def list_merged_pulls(self, repo: str, base_branch: str) -> list[JSON]:
        closed = self._paged(f"/repos/{repo}/pulls", {"state": "closed", "base": base_branch})
        return merged_in_order(closed)
