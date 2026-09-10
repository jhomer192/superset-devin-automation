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

# SessionsQueryParams.first: minimum 1, maximum 200.
MAX_PAGE = 200


class DevinClient(Protocol):
    def create_session(self, body: JSON) -> JSON: ...

    def get_session(self, session_id: str) -> JSON: ...

    def list_sessions(self, **params: Any) -> list[JSON]: ...

    def list_automations(self) -> list[JSON]: ...

    def create_automation(self, body: JSON) -> JSON: ...

    def update_automation(self, automation_id: str, body: JSON) -> JSON: ...

    def delete_automation(self, automation_id: str) -> None: ...

    def list_playbooks(self) -> list[JSON]: ...

    def create_playbook(self, body: JSON) -> JSON: ...

    def update_playbook(self, playbook_id: str, body: JSON) -> JSON: ...

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

    def _delete(self, path: str) -> Any:
        return request_json("DELETE", f"{self._org}{path}", headers=self._headers)

    def _put(self, path: str, body: JSON) -> Any:
        return request_json("PUT", f"{self._org}{path}", headers=self._headers, body=body)

    @staticmethod
    def _items(payload: Any) -> list[JSON]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("items", "sessions", "automations", "data"):
                if isinstance(payload.get(key), list):
                    return list(payload[key])
        return []

    def _paginate(self, path: str, params: dict[str, Any]) -> list[JSON]:
        """Read every page of a PaginatedResponse by following `end_cursor` into `after`.

        A single page holds at most `first` items, so a caller that reads one page silently
        loses everything past it once the window grows beyond that.
        """
        query = dict(params)
        query.setdefault("first", MAX_PAGE)
        out: list[JSON] = []
        seen: set[str] = set()
        while True:
            payload = self._get(path, query)
            out.extend(self._items(payload))
            if not isinstance(payload, dict) or not payload.get("has_next_page"):
                return out
            cursor = str(payload.get("end_cursor") or "")
            if not cursor or cursor in seen:
                return out
            seen.add(cursor)
            query["after"] = cursor

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
        return self._paginate("/sessions", params)

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

    # DELETE /v3/organizations/{org_id}/automations/{automation_id}
    def delete_automation(self, automation_id: str) -> None:
        self._delete(f"/automations/{automation_id}")

    # GET /v3/organizations/{org_id}/playbooks
    def list_playbooks(self) -> list[JSON]:
        return self._items(self._get("/playbooks"))

    # POST /v3/organizations/{org_id}/playbooks  (PlaybookCreateRequest)
    def create_playbook(self, body: JSON) -> JSON:
        result: JSON = self._post("/playbooks", body)
        return result

    # PUT /v3/organizations/{org_id}/playbooks/{playbook_id}  (same request body as create)
    def update_playbook(self, playbook_id: str, body: JSON) -> JSON:
        result: JSON = self._put(f"/playbooks/{playbook_id}", body)
        return result

    # GET /v3/organizations/{org_id}/consumption/daily/sessions/{session_id}
    def session_consumption(self, session_id: str) -> JSON:
        result: JSON = self._get(f"/consumption/daily/sessions/{session_id}")
        return result
