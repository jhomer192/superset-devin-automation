"""Runtime settings. Everything comes from environment variables; nothing is read from files."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    devin_api_base: str
    devin_api_key: str
    devin_org_id: str
    github_token: str
    target_repo: str
    automation_repo: str
    ready_label: str
    simulate: bool
    log_level: str

    @property
    def target_owner(self) -> str:
        return self.target_repo.split("/", 1)[0]

    @property
    def target_name(self) -> str:
        return self.target_repo.split("/", 1)[1]

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


def load_settings(simulate: bool = False) -> Settings:
    env = os.environ
    return Settings(
        devin_api_base=env.get("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/"),
        devin_api_key=env.get("DEVIN_API_KEY", ""),
        devin_org_id=env.get("DEVIN_ORG_ID", ""),
        github_token=env.get("GITHUB_TOKEN", ""),
        target_repo=env.get("TARGET_REPO", "jhomer192/superset"),
        automation_repo=env.get("AUTOMATION_REPO", "jhomer192/superset-devin-automation"),
        ready_label=env.get("READY_LABEL", "ready"),
        simulate=simulate or env.get("SIMULATE", "").lower() in {"1", "true", "yes"},
        log_level=env.get("LOG_LEVEL", "INFO"),
    )
