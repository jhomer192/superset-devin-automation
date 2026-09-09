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
"""Static assertions over superset-frontend/package-lock.json and package.json.

Usage: lockfile_check.py <superset_src> <rule> ...

Rules (all must hold; exit 0 only when every rule passes):
  min:<pkg>:<floor>[:<ceiling>]        every resolved copy of <pkg> is >= floor (and < ceiling)
  not-in:<pkg>:<lo>:<hi>               no resolved copy of <pkg> has lo <= version < hi
  path-min:<node_modules path>:<floor>[:<ceiling>]   the entry at that exact path is in range
  override-floor:<override key>:<dep>:<floor>
                                       package.json overrides[key][dep] (if present) is >= floor
  no-override:<override key>:<dep>     overrides[key] has no <dep> entry
  any-of:<ruleA>|<ruleB>               at least one of the two rules holds
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

VERSION_RE = re.compile(r"^\D*(\d+)\.(\d+)\.(\d+)")


def parse_version(raw: str) -> tuple[int, int, int]:
    m = VERSION_RE.match(raw)
    if not m:
        raise ValueError(f"unparseable version {raw!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def resolved_copies(lock: dict[str, Any], pkg: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, entry in lock.get("packages", {}).items():
        if path.endswith(f"node_modules/{pkg}") and "version" in entry:
            out[path] = entry["version"]
    return out


class Checker:
    def __init__(self, src: Path) -> None:
        frontend = src / "superset-frontend"
        self.lock = json.loads((frontend / "package-lock.json").read_text())
        self.pkg = json.loads((frontend / "package.json").read_text())
        self.failures: list[str] = []

    def check(self, rule: str) -> bool:
        kind, _, rest = rule.partition(":")
        if kind == "any-of":
            a, _, b = rest.partition("|")
            return self.check(a) or self.check(b)
        parts = rest.split(":")
        if kind == "min":
            return self._min(parts[0], parts[1], parts[2] if len(parts) > 2 else None)
        if kind == "not-in":
            return self._not_in(parts[0], parts[1], parts[2])
        if kind == "path-min":
            return self._path_min(parts[0], parts[1], parts[2] if len(parts) > 2 else None)
        if kind == "override-floor":
            return self._override_floor(parts[0], parts[1], parts[2])
        if kind == "no-override":
            return self._no_override(parts[0], parts[1])
        self.failures.append(f"unknown rule {rule!r}")
        return False

    def _in_range(self, version: str, floor: str, ceiling: str | None) -> bool:
        v = parse_version(version)
        if v < parse_version(floor):
            return False
        return ceiling is None or v < parse_version(ceiling)

    def _min(self, pkg: str, floor: str, ceiling: str | None) -> bool:
        copies = resolved_copies(self.lock, pkg)
        if not copies:
            self.failures.append(f"min: no resolved copies of {pkg}")
            return False
        bad = {p: v for p, v in copies.items() if not self._in_range(v, floor, ceiling)}
        if bad:
            self.failures.append(f"min: {pkg} outside [{floor},{ceiling}) at {bad}")
        return not bad

    def _not_in(self, pkg: str, lo: str, hi: str) -> bool:
        copies = resolved_copies(self.lock, pkg)
        bad = {p: v for p, v in copies.items() if self._in_range(v, lo, hi)}
        if bad:
            self.failures.append(f"not-in: vulnerable {pkg} in [{lo},{hi}) at {bad}")
        return not bad

    def _path_min(self, path: str, floor: str, ceiling: str | None) -> bool:
        entry = self.lock.get("packages", {}).get(path)
        if not entry or "version" not in entry:
            self.failures.append(f"path-min: {path} not in lockfile")
            return False
        ok = self._in_range(entry["version"], floor, ceiling)
        if not ok:
            self.failures.append(f"path-min: {path} is {entry['version']}, want [{floor},{ceiling})")
        return ok

    def _override_floor(self, key: str, dep: str, floor: str) -> bool:
        spec = self.pkg.get("overrides", {}).get(key, {})
        if not isinstance(spec, dict) or dep not in spec:
            return True
        raw = str(spec[dep])
        m = re.search(r"(\d+\.\d+\.\d+)", raw)
        if not m or parse_version(m.group(1)) < parse_version(floor):
            self.failures.append(f"override-floor: overrides[{key}][{dep}]={raw!r} admits < {floor}")
            return False
        return True

    def _no_override(self, key: str, dep: str) -> bool:
        spec = self.pkg.get("overrides", {}).get(key, {})
        if isinstance(spec, dict) and dep in spec:
            self.failures.append(f"no-override: overrides[{key}][{dep}] is present")
            return False
        return True


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    checker = Checker(Path(argv[1]))
    ok = True
    for rule in argv[2:]:
        passed = checker.check(rule)
        print(f"{'PASS' if passed else 'FAIL'} {rule}")
        ok = ok and passed
    for failure in checker.failures:
        print(f"  {failure}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
