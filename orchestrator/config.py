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
    report_digest_issue: int | None
    report_digest_every_hours: int
    acu_usd: float | None
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
    every = int(env.get("VERIFY_EVERY_N_MERGES", "1") or 1)
    if every < 1:
        raise SystemExit("VERIFY_EVERY_N_MERGES must be >= 1")
    digest_issue = env.get("REPORT_DIGEST_ISSUE", "").strip()
    digest_hours = int(env.get("REPORT_DIGEST_EVERY_HOURS", "24") or 24)
    if digest_hours < 1:
        raise SystemExit("REPORT_DIGEST_EVERY_HOURS must be >= 1")
    # The API bills in ACUs and quotes no price, so money is only reported at a rate given here.
    rate = env.get("ACU_USD", "").strip()
    return Settings(
        devin_api_base=env.get("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/"),
        # the second names are the Devin secret names this loop is provisioned with
        devin_api_key=_first(env, "DEVIN_API_KEY", "superset_remediation_bot"),
        devin_org_id=env.get("DEVIN_ORG_ID", "") or DEFAULT_ORG_ID,
        github_token=_first(env, "GITHUB_TOKEN", "superset_github_key"),
        target_repo=env.get("TARGET_REPO", "jhomer192/superset"),
        automation_repo=env.get("AUTOMATION_REPO", "jhomer192/superset-devin-automation"),
        ready_label=env.get("READY_LABEL", "ready"),
        verify_branch=env.get("VERIFY_BRANCH", "master"),
        verify_every_n_merges=every,
        report_digest_issue=int(digest_issue) if digest_issue else None,
        report_digest_every_hours=digest_hours,
        acu_usd=float(rate) if rate else None,
        simulate=simulate or env.get("SIMULATE", "").lower() in {"1", "true", "yes"},
        log_level=env.get("LOG_LEVEL", "INFO"),
    )
