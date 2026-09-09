"""Docs must be checkable against the tree."""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = tuple(
    a + b
    for a, b in (("Co-Auth", "ored-By"), ("Co-auth", "ored-by"), ("Generated ", "with"), ("Generated ", "by"))
)


def test_issues_md_is_generated_from_registry() -> None:
    assert subprocess.run([sys.executable, "verify/gen_issues_md.py", "--check"], cwd=ROOT).returncode == 0


def test_readme_paths_exist() -> None:
    readme = (ROOT / "README.md").read_text()
    for path in set(re.findall(r"`((?:orchestrator|probes|verify|fixtures|tests)/[\w./-]+)`", readme)):
        assert (ROOT / path.rstrip("/")).exists(), path


def test_env_example_has_only_empty_secrets() -> None:
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.startswith(("DEVIN_API_KEY", "DEVIN_ORG_ID", "GITHUB_TOKEN")):
            assert line.split("=", 1)[1] == ""


def test_no_ai_attribution_or_ceilings_anywhere() -> None:
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert tracked, "run from a git checkout"
    # these files name the forbidden keys in order to reject them
    ceiling_guards = {
        "orchestrator/v3_schemas.json",
        "orchestrator/automations.py",
        "orchestrator/simulate.py",
    }
    for rel in tracked:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        if rel != "orchestrator/prompts.py":
            for needle in FORBIDDEN:
                assert needle not in text, (rel, needle)
        if rel.startswith("orchestrator/") and rel not in ceiling_guards:
            assert "max_acu_limit" not in text, rel


def test_vendored_schema_cites_openapi_source() -> None:
    bundle = json.loads((ROOT / "orchestrator" / "v3_schemas.json").read_text())
    assert bundle["source"] == "https://docs.devin.ai/v3-openapi.yaml"
