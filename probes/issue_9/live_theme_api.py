# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Live probe for jhomer192/superset#9 against a running Superset.

POSTs a theme whose brandSpinnerSvg carries the nested javascript: payload, reads it back
through the API, and asserts the persisted json_data carries no javascript: substring. This is
the same path the browser hits (superset/themes/utils.py -> sanitize_svg_content) so it checks
the fix where it is actually used, not just the helper.

Env: SUPERSET_URL (default http://localhost:8088), SUPERSET_ADMIN_USER/PASSWORD (default admin/admin).
Exit 0 iff the stored theme is clean.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

PAYLOADS = [
    '<svg><a xlink:href="javajavascript:script:alert(1)"><text>x</text></a></svg>',
    '<svg><iframe src="javajavascript:script:alert(1)"></iframe></svg>',
    "<svg><foreignObject>PWNED TEXT</foreignObject></svg>",
]


def call(method: str, url: str, body: Any | None = None, token: str | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        print(f"{method} {url} -> {exc.code}: {exc.read().decode(errors='replace')[:500]}")
        raise


def main() -> int:
    base = os.environ.get("SUPERSET_URL", "http://localhost:8088").rstrip("/")
    user = os.environ.get("SUPERSET_ADMIN_USER", "admin")
    password = os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin")
    token = call(
        "POST",
        f"{base}/api/v1/security/login",
        {"username": user, "password": password, "provider": "db", "refresh": False},
    )["access_token"]

    failures = 0
    for idx, svg in enumerate(PAYLOADS):
        created = call(
            "POST",
            f"{base}/api/v1/theme/",
            {
                "theme_name": f"sda-probe-issue-9-{idx}",
                "json_data": json.dumps({"token": {"brandSpinnerSvg": svg}}),
            },
            token,
        )
        theme_id = created["id"]
        stored = call("GET", f"{base}/api/v1/theme/{theme_id}", token=token)["result"]
        json_data = stored["json_data"]
        raw = json_data if isinstance(json_data, str) else json.dumps(json_data)
        spinner = json.loads(raw).get("token", {}).get("brandSpinnerSvg", "")
        print(f"payload {idx}: stored brandSpinnerSvg={spinner!r}")
        if "javascript:" in spinner.lower() or "pwned text" in spinner.lower():
            print(f"FAIL payload {idx}: hostile content survived the theme write path")
            failures += 1
        if "foreignobject" in spinner.lower():
            print(f"FAIL payload {idx}: foreignObject survived")
            failures += 1
        call("DELETE", f"{base}/api/v1/theme/{theme_id}", token=token)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
