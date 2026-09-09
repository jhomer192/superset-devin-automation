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
"""Probe for jhomer192/superset#6 (closed as not planned): "end of this quarter" must parse.

Fails at base: handle_end_of emits LASTDAY(<date>, quarter) but the LASTDAY grammar only accepts
year|month|week, so datetime_eval raises.
"""

from __future__ import annotations

from datetime import datetime

from superset.utils.date_parser import datetime_eval, get_since_until


def test_lastday_quarter_evaluates_to_quarter_end() -> None:
    result = datetime_eval("LASTDAY(DATETIME('2024-05-17'), quarter)")
    assert result == datetime(2024, 6, 30)


def test_end_of_this_quarter_time_range_does_not_raise() -> None:
    since, until = get_since_until("end of this quarter : now", relative_end="2024-05-17")
    assert since is not None
    assert since.month in {3, 6, 9, 12}
    assert (since.month, since.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}
    assert until is not None
