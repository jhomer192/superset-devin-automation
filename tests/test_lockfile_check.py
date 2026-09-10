import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "probes" / "lockfile_check.py"


@pytest.fixture
def src(tmp_path):
    frontend = tmp_path / "superset-frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"overrides": {"minimatch@>=10": {"brace-expansion": ">=5.0.9"}}})
    )
    (frontend / "package-lock.json").write_text(
        json.dumps({"packages": {"node_modules/nx": {"version": "23.1.2"}}})
    )
    return tmp_path


def run(src, *rules):
    return subprocess.run([sys.executable, str(SCRIPT), str(src), *rules], capture_output=True, text=True)


def test_any_of_reports_nothing_when_the_second_branch_passes(src):
    r = run(
        src,
        "any-of:no-override:minimatch@>=10:brace-expansion"
        "|override-floor:minimatch@>=10:brace-expansion:5.0.9",
    )
    assert r.returncode == 0
    assert r.stderr == ""
    assert r.stdout.startswith("PASS any-of:")


def test_any_of_reports_both_branches_when_both_fail(src):
    r = run(src, "any-of:path-min:node_modules/nx:24.0.0|not-in:nx:23.0.0:24.0.0")
    assert r.returncode == 1
    assert "path-min:" in r.stderr and "not-in:" in r.stderr
    assert r.stdout.startswith("FAIL any-of:")


def test_failures_only_name_failing_rules(src):
    r = run(src, "path-min:node_modules/nx:23.1.2", "path-min:node_modules/nx:24.0.0")
    assert r.returncode == 1
    assert r.stderr.count("path-min:") == 1 and "want [24.0.0" in r.stderr
