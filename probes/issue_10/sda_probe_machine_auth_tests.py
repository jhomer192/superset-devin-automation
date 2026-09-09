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
"""Probe for jhomer192/superset#10, run in the integration lane against a seeded Postgres.

Uses a real admin user loaded by `superset load-test-users` so get_auth_cookies exercises the
genuine session machinery; only the Playwright browser context is mocked.

Acceptance criteria exercised (issue #10):
- the injected cookie still reaches add_cookies
- no priming page is left open: if new_page was called, page.close was called exactly once
- if a page was opened, close() precedes add_cookies() in call order
- the override short-circuits before any of this
Fails at base: new_page is called and close is never called.
"""

from unittest.mock import MagicMock, call, patch

from superset.extensions import machine_auth_provider_factory
from superset.utils.machine_auth import MachineAuthProvider
from tests.integration_tests.base_tests import SupersetTestCase


class MachineAuthPrimingPageProbe(SupersetTestCase):
    def test_priming_page_is_closed_before_cookies_are_injected(self) -> None:
        user = self.get_user("admin")
        provider = machine_auth_provider_factory.instance

        parent = MagicMock()
        mock_context = parent.context
        mock_page = parent.page
        mock_context.new_page.return_value = mock_page

        with patch.object(provider, "get_cookies", return_value={"session": "abc123"}):
            result = provider.authenticate_browser_context(mock_context, user)

        assert result is mock_context
        cookies_added = mock_context.add_cookies.call_args[0][0]
        assert any(c["name"] == "session" and c["value"] == "abc123" for c in cookies_added)

        if mock_context.new_page.called:
            assert mock_page.close.call_count == 1, "priming page left open"
            names = [c[0] for c in parent.mock_calls]
            assert "page.close" in names
            assert names.index("page.close") < names.index("context.add_cookies"), (
                "page closed after cookies were added"
            )

    def test_real_admin_session_cookie_is_issued(self) -> None:
        user = self.get_user("admin")
        cookies = machine_auth_provider_factory.instance.get_auth_cookies(user)
        assert cookies["session"]

    def test_override_short_circuits(self) -> None:
        user = MagicMock()
        mock_context = MagicMock()
        override = MagicMock(return_value=mock_context)
        provider = MachineAuthProvider(auth_webdriver_func_override=override)
        assert provider.authenticate_browser_context(mock_context, user) is mock_context
        assert override.mock_calls == [call(mock_context, user)]
        mock_context.new_page.assert_not_called()
