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
"""Probe for jhomer192/superset#9: sanitize_svg_content must be idempotent and not reassemble
javascript: payloads.

Acceptance criteria exercised (issue #9): the five hostile inputs, idempotency, a legitimate
static SVG, and the shipped loading.svg spinner surviving with its animation attributes.
Fails at base: the regex denylist removes the inner "javascript:" and reassembles the outer one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import superset
from superset.utils.core import sanitize_svg_content

LOADING_SVG = Path(superset.__file__).resolve().parent / "templates" / "superset" / "loading.svg"

HOSTILE = [
    '<svg><a xlink:href="javajavascript:script:alert(1)"><text>x</text></a></svg>',
    '<svg><iframe src="javajavascript:script:alert(1)"></iframe></svg>',
    '<svg><set attributeName="onload" to="alert(1)"/></svg>',
    '<svg><a href="javascript&#58;alert(1)"><text>x</text></a></svg>',
    "<svg><foreignObject>PWNED TEXT</foreignObject></svg>",
]

EXISTING_TEST_INPUTS = [
    '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="50" cy="50" r="40"/></svg>',
    "<svg><script>alert(1)</script><circle r=\"1\"/></svg>",
    '<svg><script type="text/javascript">alert(1)</script foo><circle r="1"/></svg>',
    '<svg><script>alert(1)<circle r="1"/></svg>',
]


def test_nested_javascript_in_href_does_not_reassemble() -> None:
    out = sanitize_svg_content(HOSTILE[0]).lower()
    assert "javascript:" not in out


def test_nested_javascript_in_iframe_src_does_not_reassemble() -> None:
    out = sanitize_svg_content(HOSTILE[1]).lower()
    assert "javascript:" not in out


def test_set_element_cannot_smuggle_onload() -> None:
    out = sanitize_svg_content(HOSTILE[2])
    assert not re.search(r'attributeName\s*=\s*["\']onload["\']', out, re.IGNORECASE) or (
        "alert(1)" not in out
    )
    assert "alert(1)" not in out


def test_entity_encoded_javascript_does_not_survive() -> None:
    out = sanitize_svg_content(HOSTILE[3]).lower()
    assert "javascript&#58;" not in out
    assert "javascript:" not in out


def test_foreign_object_and_its_text_are_removed() -> None:
    out = sanitize_svg_content(HOSTILE[4])
    assert "foreignObject" not in out
    assert "foreignobject" not in out.lower()
    assert "PWNED TEXT" not in out


@pytest.mark.parametrize("src", HOSTILE + EXISTING_TEST_INPUTS)
def test_idempotent(src: str) -> None:
    once = sanitize_svg_content(src)
    assert sanitize_svg_content(once) == once


def test_legitimate_static_svg_survives() -> None:
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
        "<title>Logo</title><g><circle cx=\"12\" cy=\"12\" r=\"10\" fill=\"#123456\"/>"
        '<path d="M2 2 L22 22" stroke="#000"/></g></svg>'
    )
    out = sanitize_svg_content(src)
    for needle in ("viewBox", "<title>Logo</title>", "<g>", "<circle", "<path", 'fill="#123456"'):
        assert needle in out


def test_shipped_loading_spinner_survives_with_animation() -> None:
    src = LOADING_SVG.read_text()
    out = sanitize_svg_content(src)
    for tag in ("<defs", "<filter", "<feDropShadow", "<path", "<animate", "<use"):
        assert tag in out, f"{tag} stripped from loading.svg"
    for attr in (
        "id=",
        "href=",
        "url(#",
        "attributeName=",
        "values=",
        "keyTimes=",
        "dur=",
        "repeatCount=",
        "flood-color=",
        "stdDeviation=",
        "stroke-dasharray=",
        "stroke-dashoffset=",
    ):
        assert attr in out, f"{attr} stripped from loading.svg"
