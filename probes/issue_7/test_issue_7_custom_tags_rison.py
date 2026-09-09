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
"""Probe for jhomer192/superset#7: rison `q` rewrite must be structural, not substring.

Acceptance criteria exercised (issue #7):
- filter value 'tags.name migration' reaches FAB unchanged
- a column that is already custom_tags.name is not rewritten to custom_custom_tags.name
- malformed rison is passed through unchanged
- select_columns/filters/order_column entries tags.id/name/type are rewritten to custom_tags.*
Fails at base: get_list uses str.replace on the raw q string.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import prison
from flask import Flask, Response, request

from superset.views.custom_tags_api_mixin import CustomTagsOptimizationMixin


class _Capture:
    seen_q: str | None = None

    def get_list(self, **kwargs: Any) -> Response:
        _Capture.seen_q = request.args.get("q")
        return Response("ok")


class _Probe(CustomTagsOptimizationMixin, _Capture):
    _custom_tags_only = True


def _rewrite(app: Flask, q: str) -> str | None:
    _Capture.seen_q = None
    with app.test_request_context(f"/api/v1/dashboard/?q={quote(q, safe='')}"):
        _Probe().get_list()
    return _Capture.seen_q


def _decode(q: str | None) -> Any:
    assert q is not None
    return prison.loads(q)


def test_filter_value_containing_tags_prefix_is_untouched(app: Flask) -> None:
    q = prison.dumps(
        {"filters": [{"col": "dashboard_title", "opr": "title_or_slug", "value": "tags.name migration"}]}
    )
    out = _decode(_rewrite(app, q))
    assert out["filters"][0]["value"] == "tags.name migration"
    assert out["filters"][0]["col"] == "dashboard_title"


def test_structural_fields_are_rewritten(app: Flask) -> None:
    q = prison.dumps(
        {
            "columns": ["id", "tags.id", "tags.name", "tags.type"],
            "filters": [{"col": "tags.name", "opr": "eq", "value": "owner:1"}],
            "order_column": "tags.name",
        }
    )
    out = _decode(_rewrite(app, q))
    assert out["columns"] == ["id", "custom_tags.id", "custom_tags.name", "custom_tags.type"]
    assert out["filters"][0]["col"] == "custom_tags.name"
    assert out["filters"][0]["value"] == "owner:1"
    assert out["order_column"] == "custom_tags.name"


def test_rewrite_is_idempotent(app: Flask) -> None:
    q = prison.dumps({"columns": ["custom_tags.name", "tags.id"]})
    once = _rewrite(app, q)
    assert once is not None
    twice = _rewrite(app, once)
    assert _decode(once) == _decode(twice)
    assert "custom_custom_tags" not in (twice or "")


def test_malformed_rison_passes_through(app: Flask) -> None:
    broken = "(columns:!(tags.name"
    assert _rewrite(app, broken) == broken


def test_no_q_is_untouched(app: Flask) -> None:
    _Capture.seen_q = "sentinel"
    with app.test_request_context("/api/v1/dashboard/"):
        _Probe().get_list()
    assert _Capture.seen_q is None
