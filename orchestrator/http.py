"""Minimal JSON-over-HTTPS helper on the standard library. No third-party HTTP client."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:500]}")


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    body: Any | None = None,
    retries: int = 4,
) -> Any:
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
    data = None
    hdrs = dict(headers)
    hdrs.setdefault("Accept", "application/json")
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace")
            if exc.code in RETRY_STATUSES and attempt < retries:
                attempt += 1
                delay = min(2**attempt, 30)
                log.warning("%s %s -> %s, retry %d in %ss", method, url, exc.code, attempt, delay)
                time.sleep(delay)
                continue
            raise HttpError(exc.code, url, text) from None
        except urllib.error.URLError as exc:
            if attempt < retries:
                attempt += 1
                time.sleep(min(2**attempt, 30))
                continue
            raise HttpError(0, url, str(exc.reason)) from None
