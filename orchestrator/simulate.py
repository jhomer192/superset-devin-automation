"""In-memory Devin and GitHub clients for `--simulate` and for unit tests.

Simulated sessions follow the real v3 lifecycle: a created session is `new`, `advance()` moves it
through `running` to `exit` with a structured output that is validated against the same Draft-7
schema a real session would be held to. Nothing here touches the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .github_api import merged_in_order
from .schema import validate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
JSON = dict[str, Any]


@dataclass
class FakeDevin:
    sessions: dict[str, JSON] = field(default_factory=dict)
    automations: dict[str, JSON] = field(default_factory=dict)
    playbooks: dict[str, JSON] = field(default_factory=dict)
    consumption: dict[str, float] = field(default_factory=dict)
    _n: int = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:04d}"

    def create_session(self, body: JSON) -> JSON:
        for forbidden in ("max_acu_limit",):
            if forbidden in body:
                raise AssertionError(f"session body carries {forbidden}")
        sid = self._next("devin-sim")
        self.sessions[sid] = {
            "session_id": sid,
            "status": "new",
            "status_detail": None,
            "title": body.get("title"),
            "tags": list(body.get("tags", [])),
            "prompt": body["prompt"],
            "structured_output_schema": body.get("structured_output_schema"),
            "structured_output": None,
            "pull_requests": [],
            "acus_consumed": 0.0,
            "url": f"https://app.devin.ai/sessions/{sid}",
            "origin": "automation",
        }
        return dict(self.sessions[sid])

    def get_session(self, session_id: str) -> JSON:
        return dict(self.sessions[session_id])

    def list_sessions(self, **params: Any) -> list[JSON]:
        origins = params.get("origins")
        tags = {str(t) for t in params.get("tags") or []}
        return [
            dict(s)
            for s in self.sessions.values()
            if (not origins or s["origin"] == origins) and (not tags or tags & set(s["tags"]))
        ]

    def list_automations(self) -> list[JSON]:
        return [dict(a) for a in self.automations.values()]

    def create_automation(self, body: JSON) -> JSON:
        aid = self._next("auto-sim")
        self.automations[aid] = {"automation_id": aid, **body}
        return dict(self.automations[aid])

    def update_automation(self, automation_id: str, body: JSON) -> JSON:
        self.automations[automation_id].update(body)
        return dict(self.automations[automation_id])

    def delete_automation(self, automation_id: str) -> None:
        del self.automations[automation_id]

    def list_playbooks(self) -> list[JSON]:
        return [dict(p) for p in self.playbooks.values()]

    def create_playbook(self, body: JSON) -> JSON:
        pid = self._next("playbook-sim")
        self.playbooks[pid] = {"playbook_id": pid, **body}
        return dict(self.playbooks[pid])

    def update_playbook(self, playbook_id: str, body: JSON) -> JSON:
        self.playbooks[playbook_id].update(body)
        return dict(self.playbooks[playbook_id])

    def session_metrics(self, time_after: int, time_before: int) -> JSON:
        sessions = list(self.sessions.values())
        merged = [s for s in sessions if any(p.get("pr_state") == "merged" for p in s["pull_requests"])]
        acus = [float(s["acus_consumed"]) for s in sessions]
        return {
            "sessions_created_count": len(sessions),
            "sessions_with_merged_prs_count": len(merged),
            "avg_acus_per_session": round(sum(acus) / len(acus), 3) if acus else 0.0,
        }

    def pr_metrics(self, time_after: int, time_before: int) -> JSON:
        prs = [pr for s in self.sessions.values() for pr in s["pull_requests"]]
        states = [p.get("pr_state") for p in prs]
        return {
            "prs_created_count": len(prs),
            "prs_opened_count": states.count("open"),
            "prs_merged_count": states.count("merged"),
            "prs_closed_count": states.count("closed"),
        }

    def org_consumption(self, time_after: int, time_before: int) -> JSON:
        return {"total_acus": round(sum(self.consumption.values()), 3), "consumption_by_date": []}

    def session_consumption(self, session_id: str) -> JSON:
        return {"session_id": session_id, "total_acus": self.consumption.get(session_id, 0.0)}

    # -- simulation controls -------------------------------------------------------------

    def advance(self, session_id: str, *, outcome: str, acus: float, pr_url: str | None = None) -> JSON:
        """Drive a session to a terminal state with a schema-valid structured output."""
        s = self.sessions[session_id]
        schema = s["structured_output_schema"]
        kind = "fix" if "sda-fix" in s["tags"] else "verify"
        issue = next((int(t.split("-", 1)[1]) for t in s["tags"] if t.startswith("issue-")), 5)
        out = _outcome(kind, outcome, pr_url, issue)
        errors = validate(schema, out)
        if errors:
            raise AssertionError(f"simulated output violates schema: {errors}")
        s["structured_output"] = out
        s["acus_consumed"] = acus
        self.consumption[session_id] = acus
        if outcome == "error":
            s["status"], s["status_detail"] = "error", None
        else:
            s["status"], s["status_detail"] = "exit", None
        if pr_url:
            s["pull_requests"] = [{"pr_url": pr_url, "pr_state": "merged" if outcome == "ok" else "open"}]
        return dict(s)

    def suspend(self, session_id: str, detail: str) -> None:
        self.sessions[session_id]["status"] = "suspended"
        self.sessions[session_id]["status_detail"] = detail


def _outcome(kind: str, outcome: str, pr_url: str | None, issue: int) -> JSON:
    met = outcome == "ok"
    if kind == "fix":
        if outcome == "error":
            return {
                "status": "error",
                "issue": issue,
                "error_message": "simulated failure before any probe ran",
            }
        out: JSON = {
            "status": "pr_opened" if pr_url else "no_change_needed",
            "issue": issue,
            "acceptance_met": met,
            "probe_command": f"probes/run.sh issue_{issue}/unit",
            "probe_exit_code": 0 if met else 1,
            "base_probe_exit_code": 1,
            "evidence": "simulated: probe failed at base, passed at head"
            if met
            else "simulated: probe still failing",
        }
        if pr_url:
            out["pr_url"] = pr_url
            out["branch"] = f"devin/fix-issue-{issue}"
        return out
    head = "1111111111111111111111111111111111111111"
    if outcome == "error":
        return {"status": "error", "head_sha": head, "error_message": "simulated: postgres failed to start"}
    return {
        "status": "ok",
        "acceptance_met": met,
        "probe_command": "verify/run_all.sh --head <sha> --base <sha> --issues 5",
        "probe_exit_code": 0 if met else 1,
        "evidence": "simulated run_all.sh result",
        "head_sha": head,
        "base_sha": "fc110d8428f35249a2092778ca0a3e26a2de0b14",
        "results": [
            {
                "issue": issue,
                "probe": f"issue_{issue}/unit",
                "kind": "offline_pytest",
                "base_exit_code": 1,
                "head_exit_code": 0 if met else 1,
                "acceptance_met": met,
                "evidence": "simulated pytest summary",
            }
        ],
    }


@dataclass
class FakeGitHub:
    issues: dict[int, JSON] = field(default_factory=dict)
    comments: dict[int, list[JSON]] = field(default_factory=dict)
    pulls: dict[int, JSON] = field(default_factory=dict)
    parents: dict[str, list[str]] = field(default_factory=dict)
    branch_heads: dict[str, str] = field(default_factory=dict)
    _n: int = 0

    @classmethod
    def from_fixtures(cls, root: Path = FIXTURES) -> FakeGitHub:
        gh = cls()
        for issue in json.loads((root / "issues.json").read_text())["issues"]:
            gh.issues[int(issue["number"])] = issue
        event = json.loads((root / "merged_pr_event.json").read_text())
        pr = event["pull_request"]
        gh.pulls[int(pr["number"])] = pr
        gh.parents[pr["merge_commit_sha"]] = [pr["base"]["sha"]]
        gh.branch_heads[pr["base"]["ref"]] = pr["merge_commit_sha"]
        return gh

    def merge(self, number: int, *, branch: str, sha: str, title: str = "", body: str = "") -> JSON:
        """Record PR `number` as merged into `branch`, on top of the current branch head."""
        self._n += 1
        pr = {
            "number": number,
            "html_url": f"https://github.com/jhomer192/superset/pull/{number}",
            "title": title,
            "body": body,
            "state": "closed",
            "merged": True,
            "merged_at": f"2026-01-01T00:00:{self._n:02d}Z",
            "merge_commit_sha": sha,
            "base": {"ref": branch, "sha": self.branch_heads.get(branch, "")},
        }
        self.parents[sha] = [self.branch_heads[branch]] if branch in self.branch_heads else []
        self.branch_heads[branch] = sha
        self.pulls[number] = pr
        return dict(pr)

    def list_issues(self, repo: str, labels: str, state: str = "open") -> list[JSON]:
        wanted = {lbl.strip() for lbl in labels.split(",") if lbl.strip()}
        return [
            dict(i)
            for i in self.issues.values()
            if (state == "all" or i["state"] == state)
            and wanted <= {lb["name"] for lb in i.get("labels", [])}
        ]

    def get_issue(self, repo: str, number: int) -> JSON:
        return dict(self.issues[number])

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> JSON:
        number = max([*self.issues, *self.pulls], default=0) + 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": name} for name in labels],
            "html_url": f"https://github.com/{repo}/issues/{number}",
        }
        return dict(self.issues[number])

    def list_issue_comments(self, repo: str, number: int) -> list[JSON]:
        return [dict(c) for c in self.comments.get(number, [])]

    def create_issue_comment(self, repo: str, number: int, body: str) -> JSON:
        self._n += 1
        comment = {"id": self._n, "body": body, "issue_number": number}
        self.comments.setdefault(number, []).append(comment)
        return dict(comment)

    def list_pulls(self, repo: str, state: str = "open") -> list[JSON]:
        return [dict(p) for p in self.pulls.values() if state == "all" or p.get("state") == state]

    def get_pull(self, repo: str, number: int) -> JSON:
        return dict(self.pulls[number])

    def list_merged_pulls(self, repo: str, base_branch: str) -> list[JSON]:
        return merged_in_order(
            [dict(p) for p in self.pulls.values() if (p.get("base") or {}).get("ref") == base_branch]
        )

    def get_commit_parents(self, repo: str, sha: str) -> list[str]:
        return list(self.parents.get(sha, []))

    def label(self, number: int, name: str) -> None:
        labels = self.issues[number].setdefault("labels", [])
        if not any(lb["name"] == name for lb in labels):
            labels.append({"name": name})

    def close_completed(self, number: int) -> None:
        self.issues[number]["state"] = "closed"
        self.issues[number]["state_reason"] = "completed"


def load_event(path: Path | None = None) -> JSON:
    data: JSON = json.loads((path or FIXTURES / "merged_pr_event.json").read_text())
    return data


_PR_NUMBER = re.compile(r"/pull/(\d+)$")


def pr_number(url: str) -> int:
    m = _PR_NUMBER.search(url)
    if not m:
        raise ValueError(f"not a PR url: {url}")
    return int(m.group(1))


def issue_event(issue: JSON, repo: str, action: str = "labeled", label: str = "sda-regression") -> JSON:
    """The github:issues payload GitHub sends when `label` is put on `issue`."""
    return {
        "action": action,
        "issue": dict(issue),
        "label": {"name": label},
        "repository": {"full_name": repo},
    }
