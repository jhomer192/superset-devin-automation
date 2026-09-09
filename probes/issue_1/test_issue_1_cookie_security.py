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
"""Probe for jhomer192/superset#1: SameSite=None without Secure must be rejected at startup.

Acceptance criteria exercised (issue #1, "Acceptance criteria"):
- None/False on a non-test app -> SystemExit(1) and the log names both keys
- None/True starts clean; shipped defaults (Lax/False/http) start clean
- PREFERRED_URL_SCHEME=https with Secure=False -> WARNING record containing SESSION_COOKIE_SECURE
- TESTING/debug: invalid combination warns, does not exit
- websocket pair checked only when WEBSOCKET_ENABLE
- shipped defaults unchanged
Fails at base: SupersetAppInitializer has no check_cookie_security.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from superset.initialization import SupersetAppInitializer

BASE_CONFIG: dict[str, Any] = {
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_SECURE": False,
    "ENABLE_PROXY_FIX": False,
    "WEBSOCKET_ENABLE": False,
    "WEBSOCKET_JWT_COOKIE_SAMESITE": None,
    "WEBSOCKET_JWT_COOKIE_SECURE": False,
}


def _initializer(
    overrides: dict[str, Any], *, debug: bool = False, testing: bool = False, scheme: str = "http"
) -> SupersetAppInitializer:
    init = object.__new__(SupersetAppInitializer)
    config = {**BASE_CONFIG, **overrides}
    init.config = config
    app = MagicMock()
    app.debug = debug
    app.config = {**config, "TESTING": testing, "PREFERRED_URL_SCHEME": scheme}
    init.superset_app = app
    return init


def _run(init: SupersetAppInitializer, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="superset.initialization")
    with patch("superset.initialization.is_test", return_value=False):
        init.check_cookie_security()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_samesite_none_without_secure_exits(caplog: pytest.LogCaptureFixture) -> None:
    init = _initializer({"SESSION_COOKIE_SAMESITE": "None", "SESSION_COOKIE_SECURE": False})
    with pytest.raises(SystemExit) as exc:
        _run(init, caplog)
    assert exc.value.code == 1
    joined = "\n".join(_warnings(caplog) + [r.getMessage() for r in caplog.records])
    assert "SESSION_COOKIE_SAMESITE" in joined
    assert "SESSION_COOKIE_SECURE" in joined


def test_samesite_none_with_secure_is_clean(caplog: pytest.LogCaptureFixture) -> None:
    init = _initializer({"SESSION_COOKIE_SAMESITE": "None", "SESSION_COOKIE_SECURE": True})
    _run(init, caplog)
    assert _warnings(caplog) == []


def test_shipped_defaults_are_clean(caplog: pytest.LogCaptureFixture) -> None:
    _run(_initializer({}), caplog)
    assert _warnings(caplog) == []


def test_https_scheme_without_secure_warns_and_does_not_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    init = _initializer({"SESSION_COOKIE_SECURE": False}, scheme="https")
    _run(init, caplog)
    assert any("SESSION_COOKIE_SECURE" in msg for msg in _warnings(caplog))


def test_proxy_fix_without_secure_warns(caplog: pytest.LogCaptureFixture) -> None:
    init = _initializer({"SESSION_COOKIE_SECURE": False, "ENABLE_PROXY_FIX": True})
    _run(init, caplog)
    assert any("SESSION_COOKIE_SECURE" in msg for msg in _warnings(caplog))


@pytest.mark.parametrize("flag", ["debug", "testing"])
def test_invalid_combination_only_warns_under_debug_or_testing(
    caplog: pytest.LogCaptureFixture, flag: str
) -> None:
    init = _initializer(
        {"SESSION_COOKIE_SAMESITE": "None", "SESSION_COOKIE_SECURE": False},
        debug=flag == "debug",
        testing=flag == "testing",
    )
    _run(init, caplog)
    assert any("SESSION_COOKIE_SECURE" in msg for msg in _warnings(caplog))


def test_websocket_pair_checked_when_enabled(caplog: pytest.LogCaptureFixture) -> None:
    init = _initializer(
        {
            "WEBSOCKET_ENABLE": True,
            "WEBSOCKET_JWT_COOKIE_SAMESITE": "None",
            "WEBSOCKET_JWT_COOKIE_SECURE": False,
        }
    )
    with pytest.raises(SystemExit) as exc:
        _run(init, caplog)
    assert exc.value.code == 1
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "WEBSOCKET_JWT_COOKIE_SAMESITE" in joined
    assert "WEBSOCKET_JWT_COOKIE_SECURE" in joined


def test_websocket_pair_ignored_when_disabled(caplog: pytest.LogCaptureFixture) -> None:
    init = _initializer(
        {
            "WEBSOCKET_ENABLE": False,
            "WEBSOCKET_JWT_COOKIE_SAMESITE": "None",
            "WEBSOCKET_JWT_COOKIE_SECURE": False,
        }
    )
    _run(init, caplog)
    assert _warnings(caplog) == []


def test_shipped_cookie_defaults_unchanged() -> None:
    from superset import config

    assert config.SESSION_COOKIE_SECURE is False
    assert config.SESSION_COOKIE_SAMESITE == "Lax"
    assert config.WEBSOCKET_JWT_COOKIE_SECURE is False
    assert config.WEBSOCKET_JWT_COOKIE_SAMESITE is None


def test_check_is_wired_into_init_app() -> None:
    import inspect

    src = inspect.getsource(SupersetAppInitializer.init_app)
    assert "check_cookie_security" in src
