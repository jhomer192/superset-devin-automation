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
"""Probe for jhomer192/superset#11: two adhoc filters on one column must diff correctly.

Acceptance criteria exercised (issue #11), all via the public diff_slice_params entry point.
Fails at base: duplicate natural keys overwrite each other in _diff_list_by_natural_key.
"""

from __future__ import annotations

from typing import Any

from superset.versioning.diff import ChangeRecord, diff_slice_params


def _f(subject: str, operator: str, comparator: Any) -> dict[str, Any]:
    return {
        "expressionType": "SIMPLE",
        "clause": "WHERE",
        "subject": subject,
        "operator": operator,
        "comparator": comparator,
    }


GT1 = _f("sales", ">", 1)
LT10 = _f("sales", "<", 10)
NE7 = _f("sales", "!=", 7)
REGION = _f("region", "==", "EMEA")


def _diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[ChangeRecord]:
    return diff_slice_params({"adhoc_filters": before}, {"adhoc_filters": after})


def _paths_are_schema_compatible(records: list[ChangeRecord]) -> None:
    for rec in records:
        assert isinstance(rec.path, list)
        assert all(isinstance(p, str) for p in rec.path), rec.path


def test_deleting_first_of_two_same_column_filters_yields_one_remove() -> None:
    records = _diff([GT1, LT10], [LT10])
    assert [r.operation for r in records] == ["remove"]
    assert records[0].from_value == GT1
    _paths_are_schema_compatible(records)


def test_editing_first_of_two_same_column_filters_yields_one_edit() -> None:
    edited = _f("sales", ">", 2)
    records = _diff([GT1, LT10], [edited, LT10])
    assert [r.operation for r in records] == ["edit"]
    assert records[0].from_value == GT1
    assert records[0].to_value == edited
    _paths_are_schema_compatible(records)


def test_adding_third_same_column_filter_yields_only_an_add() -> None:
    records = _diff([GT1, LT10], [GT1, LT10, NE7])
    assert [r.operation for r in records] == ["add"]
    assert records[0].to_value == NE7
    _paths_are_schema_compatible(records)


def test_reordering_filters_on_different_columns_yields_nothing() -> None:
    assert _diff([GT1, REGION], [REGION, GT1]) == []


def test_single_filter_edit_keeps_subject_path() -> None:
    edited = _f("sales", ">", 5)
    records = _diff([GT1], [edited])
    assert [r.operation for r in records] == ["edit"]
    assert records[0].path == ["params", "adhoc_filters", "sales"]


def test_deleting_one_of_two_different_column_filters_yields_one_remove() -> None:
    records = _diff([GT1, REGION], [GT1])
    assert [r.operation for r in records] == ["remove"]
    assert records[0].from_value == REGION
