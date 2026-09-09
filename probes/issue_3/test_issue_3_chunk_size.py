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
"""Probe for jhomer192/superset#3: one config key drives the streaming-export chunk size.

Acceptance criteria exercised:
- one config key is read at both superset/charts/data/api.py and superset/sqllab/api.py
- with the key unset both sites pass 1024 (not the command default of 1000)
- an overridden value reaches the command at both sites
- both "TODO: Make chunk size configurable" comments are gone
Fails at base: no such key exists and both sites pass a literal 1024 regardless of config.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import superset.charts.data.api as charts_api
import superset.sqllab.api as sqllab_api
from superset import config as superset_config

TODO_TEXT = "TODO: Make chunk size configurable via SUPERSET_CONFIG"
SRC_ROOT = Path(superset_config.__file__).resolve().parent.parent


def _discover_key() -> str:
    """The config key both API modules read for the chunk size."""
    config_src = (SRC_ROOT / "superset" / "config.py").read_text()
    charts_src = inspect.getsource(charts_api)
    sqllab_src = inspect.getsource(sqllab_api)
    candidates = [
        name
        for name in re.findall(r"^([A-Z][A-Z0-9_]*CHUNK[A-Z0-9_]*)\s*[:=]", config_src, re.M)
        if name in charts_src and name in sqllab_src
    ]
    assert candidates, "no config key containing CHUNK is read by both API sites"
    assert len(set(candidates)) == 1, f"expected one shared key, found {sorted(set(candidates))}"
    return candidates[0]


def test_todo_comments_removed() -> None:
    assert TODO_TEXT not in inspect.getsource(charts_api)
    assert TODO_TEXT not in inspect.getsource(sqllab_api)


def _chart_chunk_size(app: Flask, cmd_cls: str) -> Any:
    with patch.object(charts_api, cmd_cls) as command:
        command.return_value.run.return_value = lambda: iter([b""])
        view = object.__new__(charts_api.ChartDataRestApi)
        with app.test_request_context():
            view._create_streaming_csv_response(
                {"query_context": MagicMock(form_data={})}, filename="probe.csv"
            )
    return command.call_args.args[1]


def _sqllab_chunk_size(app: Flask, cmd_cls: str) -> Any:
    with patch.object(sqllab_api, cmd_cls) as command:
        command.return_value.run.return_value = lambda: iter([b""])
        view = object.__new__(sqllab_api.SqlLabRestApi)
        with app.test_request_context():
            view._create_streaming_csv_response("client-1", filename="probe.csv")
    return command.call_args.args[1]


@pytest.fixture
def key(app: Flask) -> str:
    name = _discover_key()
    app.config.pop(name, None)
    return name


def test_default_is_1024_at_both_sites(app: Flask, key: str) -> None:
    assert _chart_chunk_size(app, "StreamingCSVExportCommand") == 1024
    assert _sqllab_chunk_size(app, "StreamingSqlResultExportCommand") == 1024


def test_override_reaches_both_sites(app: Flask, key: str) -> None:
    app.config[key] = 77
    assert _chart_chunk_size(app, "StreamingCSVExportCommand") == 77
    assert _sqllab_chunk_size(app, "StreamingSqlResultExportCommand") == 77


def test_key_is_backend_only() -> None:
    from superset.views import base as views_base

    assert _discover_key() not in inspect.getsource(views_base)
