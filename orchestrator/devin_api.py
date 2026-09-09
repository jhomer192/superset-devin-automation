"""Devin v3 API client.

Endpoint shapes follow the public OpenAPI document at https://docs.devin.ai/v3-openapi.yaml.
Every method maps to exactly one documented route; nothing here is inferred.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .http import request_json

log = logging.getLogger(__name__)

JSON = dict[str, Any]


class DevinClient(Protocol):
    def create_session(self, body: JSON) -> JSON: ...

    def get_session(self, session_id: str) -> JSON: ...

    def list_sessions(self, **params: Any) -> list[JSON]: ...

    def list_automations(self) -> list[JSON]: ...

    def create_automation(self, body: JSON) -> JSON: ...

    def update_automation(self, automation_id: str, body: JSON) -> JSON: ...

    def session_metrics(self, time_after: int, time_before: int) -> JSON: ...

    def sessions_insights(self, **params: Any) -> list[JSON]: ...

    def session_consumption(self, session_id: str) -> JSON: ...


class LiveDevinClient:
    def __init__(self, base_url: str, api_key: str, org_id: str) -> None:
        self._org = f"{base_url}/v3/organizations/{org_id}"
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return request_json("GET", f"{self._org}{path}", headers=self._headers, params=params)

    def _post(self, path: str, body: JSON) -> Any:
        return request_json("POST", f"{self._org}{path}", headers=self._headers, body=body)

    def _patch(self, path: str, body: JSON) -> Any:
        return request_json("PATCH", f"{self._org}{path}", headers=self._headers, body=body)

    @staticmethod
    def _items(payload: Any) -> list[JSON]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("items", "sessions", "automations", "data"):
                if isinstance(payload.get(key), list):
                    return list(payload[key])
        return []

    # POST /v3/organizations/{org_id}/sessions
    def create_session(self, body: JSON) -> JSON:
        result: JSON = self._post("/sessions", body)
        return result

    # GET /v3/organizations/{org_id}/sessions/{devin_id}
    def get_session(self, session_id: str) -> JSON:
        result: JSON = self._get(f"/sessions/{session_id}")
        return result

    # GET /v3/organizations/{org_id}/sessions  (SessionsQueryParams, flattened into the query)
    def list_sessions(self, **params: Any) -> list[JSON]:
        params.setdefault("first", 100)
        return self._items(self._get("/sessions", params))

    # GET /v3/organizations/{org_id}/automations
    def list_automations(self) -> list[JSON]:
        return self._items(self._get("/automations"))

    # POST /v3/organizations/{org_id}/automations
    def create_automation(self, body: JSON) -> JSON:
        result: JSON = self._post("/automations", body)
        return result

    # PATCH /v3/organizations/{org_id}/automations/{automation_id}
    def update_automation(self, automation_id: str, body: JSON) -> JSON:
        result: JSON = self._patch(f"/automations/{automation_id}", body)
        return result

    # GET /v3/organizations/{org_id}/metrics/sessions  (time_after/time_before are required ints)
    def session_metrics(self, time_after: int, time_before: int) -> JSON:
        result: JSON = self._get("/metrics/sessions", {"time_after": time_after, "time_before": time_before})
        return result

    # GET /v3/organizations/{org_id}/sessions/insights
    def sessions_insights(self, **params: Any) -> list[JSON]:
        params.setdefault("first", 100)
        return self._items(self._get("/sessions/insights", params))

    # GET /v3/organizations/{org_id}/consumption/daily/sessions/{session_id}
    def session_consumption(self, session_id: str) -> JSON:
        result: JSON = self._get(f"/consumption/daily/sessions/{session_id}")
        return result
