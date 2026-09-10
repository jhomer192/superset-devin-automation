"""Session state taxonomy over the v3 ``status`` / ``status_detail`` pair.

``status`` enum (SessionResponse.status in https://docs.devin.ai/v3-openapi.yaml):
new, claimed, running, exit, error, suspended, resuming.
``status_detail`` is a separate field, only populated on get/list responses.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from .devin_api import DevinClient

log = logging.getLogger(__name__)
POLL_SECONDS = 60

LIVE_STATUSES = frozenset({"new", "claimed", "running", "resuming"})
DEAD_STATUSES = frozenset({"exit", "error"})

# suspended + one of these: a human can still unblock the session, so it counts as live.
AWAITING_HUMAN_DETAILS = frozenset({"waiting_for_user", "waiting_for_approval", "inactivity"})
# suspended + one of these: nobody will resume it; treat as finished.
TERMINAL_SUSPENDED_DETAILS = frozenset(
    {
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
        "org_usage_limit_exceeded",
        "user_usage_limit_exceeded",
        "total_session_limit_exceeded",
    }
)


class Liveness(StrEnum):
    LIVE = "live"
    AWAITING_HUMAN = "awaiting_human"
    DEAD = "dead"
    TERMINAL_SUSPENDED = "terminal_suspended"
    UNKNOWN_SUSPENDED = "unknown_suspended"


def classify(status: str | None, status_detail: str | None) -> Liveness:
    if status in LIVE_STATUSES:
        return Liveness.LIVE
    if status in DEAD_STATUSES:
        return Liveness.DEAD
    if status == "suspended":
        if status_detail in AWAITING_HUMAN_DETAILS:
            return Liveness.AWAITING_HUMAN
        if status_detail in TERMINAL_SUSPENDED_DETAILS:
            return Liveness.TERMINAL_SUSPENDED
        return Liveness.UNKNOWN_SUSPENDED
    raise ValueError(f"unknown session status {status!r}")


def is_finished(status: str | None, status_detail: str | None) -> bool:
    """True when no further progress will come from the session without a new one."""
    return classify(status, status_detail) in {Liveness.DEAD, Liveness.TERMINAL_SUSPENDED}


def holds_slot(status: str | None, status_detail: str | None) -> bool:
    """True when the session should block a new one for the same unit of work.

    Live sessions and human-blocked sessions both hold the slot; an unknown suspended detail is
    treated as holding the slot so that a new API status_detail cannot cause duplicate PRs.
    """
    return not is_finished(status, status_detail)


def wait_until_finished(
    devin: DevinClient, session_id: str, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any]:
    """Poll until the session has written its structured output or is finished. No deadline: a
    verification takes as long as building and booting Superset takes, and a session that is
    waiting on a human without a result holds the slot until that human acts."""
    while True:
        session = devin.get_session(session_id)
        if session.get("structured_output") or is_finished(
            session.get("status"), session.get("status_detail")
        ):
            return session
        log.info("%s is %s/%s, waiting", session_id, session.get("status"), session.get("status_detail"))
        sleep(POLL_SECONDS)
