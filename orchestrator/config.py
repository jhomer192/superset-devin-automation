"""Runtime settings. Everything comes from environment variables; nothing is read from files."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# Organisation id is an identifier, not a credential; the automations are registered in it.
DEFAULT_ORG_ID = "org-cd02d46007034cdbaad11ff0d2392fac"


@dataclass(frozen=True)
class Settings:
    devin_api_base: str
    devin_api_key: str
    devin_org_id: str
    github_token: str
    target_repo: str
    automation_repo: str
    ready_label: str
    verify_branch: str
    verify_every_n_merges: int
    regression_issue_window_hours: int
    playbook_id_fix: str | None
    playbook_id_verify: str | None
    simulate: bool
    log_level: str

    def require_live(self) -> None:
        missing = [
            name
            for name, value in (
                ("DEVIN_API_KEY", self.devin_api_key),
                ("DEVIN_ORG_ID", self.devin_org_id),
                ("GITHUB_TOKEN", self.github_token),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "missing required environment variables: "
                + ", ".join(missing)
                + " (or pass --simulate to run against fixtures)"
            )


def _first(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        if env.get(name):
            return env[name]
    return ""


def load_settings(simulate: bool = False) -> Settings:
    env = os.environ
    every = int(env.get("VERIFY_EVERY_N_MERGES", "5") or 5)
    if every < 1:
        raise SystemExit("VERIFY_EVERY_N_MERGES must be >= 1")
    window_hours = int(env.get("REGRESSION_ISSUE_WINDOW_HOURS", "24") or 24)
    if window_hours < 1:
        raise SystemExit("REGRESSION_ISSUE_WINDOW_HOURS must be >= 1")
    return Settings(
        devin_api_base=env.get("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/"),
        # the second names are the Devin secret names this loop is provisioned with
        devin_api_key=_first(env, "DEVIN_API_KEY", "superset_remediation_bot", "SUPERSET_REMEDIATION_BOT"),
        devin_org_id=env.get("DEVIN_ORG_ID", "") or DEFAULT_ORG_ID,
        github_token=_first(env, "GITHUB_TOKEN", "superset_github", "SUPERSET_GITHUB", "superset_github_key"),
        target_repo=env.get("TARGET_REPO", "jhomer192/superset"),
        automation_repo=env.get("AUTOMATION_REPO", "jhomer192/superset-devin-automation"),
        ready_label=env.get("READY_LABEL", "ready"),
        verify_branch=env.get("VERIFY_BRANCH", "master"),
        verify_every_n_merges=every,
        regression_issue_window_hours=window_hours,
        playbook_id_fix=env.get("PLAYBOOK_ID_FIX") or None,
        playbook_id_verify=env.get("PLAYBOOK_ID_VERIFY") or None,
        simulate=simulate or env.get("SIMULATE", "").lower() in {"1", "true", "yes"},
        log_level=env.get("LOG_LEVEL", "INFO"),
    )
