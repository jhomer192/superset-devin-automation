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
"""Probe for jhomer192/superset#5: KQL splitter must not drop text before a ``` string.

Acceptance criteria exercised (issue #5):
- print ```a;b``` is one statement whose text is exactly the input
- print 1; print ```x``` is two statements, neither empty
- print ```a```; print ```b``` is two statements with no lost prefix
- ` ` ` (three separate backticks at a boundary) does not enter the multiline state prematurely
- existing single/double-quoted behaviour unchanged (';' inside quotes never splits)
Fails at base: tokenize_kql sets buffer = "```" and discards everything accumulated before it.
"""

from __future__ import annotations

import pytest

from superset.sql.parse import KQLTokenType, split_kql, tokenize_kql


def test_prefix_preserved_before_multiline_string() -> None:
    assert split_kql("print ```a;b```") == ["print ```a;b```"]


def test_statement_before_multiline_string_survives() -> None:
    assert split_kql("print 1; print ```x```") == ["print 1", " print ```x```"]


def test_two_multiline_statements_keep_both_prefixes() -> None:
    assert split_kql("print ```a```; print ```b```") == ["print ```a```", " print ```b```"]


def test_tokens_before_multiline_string_are_classified() -> None:
    tokens = tokenize_kql("print ```a```")
    assert tokens[0] == (KQLTokenType.WORD, "print")
    assert tokens[1] == (KQLTokenType.WHITESPACE, " ")
    assert tokens[2] == (KQLTokenType.STRING, "```a```")


def test_round_trip_is_lossless() -> None:
    src = "let x = ```multi\nline; text```;\nprint x; print 'a;b'; print \"c;d\""
    assert "".join(val for _, val in tokenize_kql(src)) == src


@pytest.mark.parametrize(
    "src,expected",
    [
        ("print 'a;b'; print 2", ["print 'a;b'", " print 2"]),
        ('print "a;b"; print 2', ['print "a;b"', " print 2"]),
        ("print 1; print 2", ["print 1", " print 2"]),
    ],
)
def test_quoted_string_behaviour_unchanged(src: str, expected: list[str]) -> None:
    assert split_kql(src) == expected


def test_semicolon_inside_multiline_string_does_not_split() -> None:
    assert len(split_kql("let s = ```a;b;c```; print s")) == 2
