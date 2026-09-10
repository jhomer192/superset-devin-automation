"""Turn probe exit codes into the verification verdict (orchestrator.schema.VERIFICATION_SCHEMA).

Rules, applied mechanically:
  * a probe for an issue in --issues (closed by the PR) is accepted iff head == 0 and base != 0
  * a probe for an issue in --regression (already landed) is accepted iff head == 0
  * a probe of a PRD requirement in --requirements is accepted iff head == 0, unless the probe
    also belongs to an issue in --issues (then the closed-issue rule applies)
  * acceptance_met is the AND over every probe; no probes at all => acceptance_met is false
    (a PR that closes nothing in the registry still gets a boot/build verdict, but it cannot
    claim an acceptance it never demonstrated)
Exit code: 0 iff acceptance_met.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.registry import Registry, load_registry  # noqa: E402
from orchestrator.schema import VERIFICATION_SCHEMA, validate  # noqa: E402

MAX_EVIDENCE = 4000


def _tail(path: str, limit: int = MAX_EVIDENCE) -> str:
    try:
        text = Path(path).read_text(errors="replace")
    except OSError as exc:
        return f"<log unreadable: {exc}>"
    return text[-limit:] if len(text) > limit else text or "<empty log>"


def build_results(
    rows: list[dict[str, Any]],
    closed: set[int],
    regression: set[int],
    requirements: set[str] | None = None,
    registry: Registry | None = None,
) -> list[dict[str, Any]]:
    guarded_by: dict[str, list[str]] = {}
    if registry is not None and requirements:
        for req in registry.requirements:
            if req.id in requirements:
                for pid in req.probes:
                    guarded_by.setdefault(pid, []).append(req.id)
    by_probe: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_probe.setdefault(row["probe"], {"issue": row["issue"], "kind": row["kind"]})
        entry[row["role"]] = row
    results: list[dict[str, Any]] = []
    for probe_id, entry in by_probe.items():
        head = entry.get("head")
        base = entry.get("base")
        head_code = int(head["exit_code"]) if head else 255
        base_code = int(base["exit_code"]) if base else None
        issue = int(entry["issue"])
        reqs = guarded_by.get(probe_id, [])
        if issue in closed:
            met = head_code == 0 and base_code not in (None, 0)
        elif issue in regression or reqs:
            met = head_code == 0
        else:
            met = False
        evidence = f"HEAD exit {head_code}:\n{_tail(head['log']) if head else '<not run>'}"
        if base:
            evidence += f"\n\nBASE exit {base_code}:\n{_tail(base['log'], 1500)}"
        results.append(
            {
                "issue": issue,
                "probe": probe_id,
                "kind": entry["kind"],
                "head_exit_code": head_code,
                "base_exit_code": base_code,
                "acceptance_met": met,
                "evidence": evidence,
                "requirements": reqs,
            }
        )
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes")
    ap.add_argument("--issues", default="")
    ap.add_argument("--regression", default="")
    ap.add_argument("--requirements", default="")
    ap.add_argument("--head", required=True)
    ap.add_argument("--base", default="")
    ap.add_argument("--health-url", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--error", action="store_true")
    ap.add_argument("--stage", default="")
    ap.add_argument("--message", default="")
    args = ap.parse_args()

    payload: dict[str, Any]
    if args.error:
        payload = {
            "status": "error",
            "head_sha": args.head,
            "base_sha": args.base or None,
            "error_message": f"stage {args.stage} failed: {args.message}",
        }
    else:
        rows = [json.loads(line) for line in Path(args.probes).read_text().splitlines() if line.strip()]
        closed = {int(x) for x in args.issues.split(",") if x.strip()}
        regression = {int(x) for x in args.regression.split(",") if x.strip()}
        requirements = {x.strip() for x in args.requirements.split(",") if x.strip()}
        results = build_results(rows, closed, regression, requirements, load_registry())
        met = bool(results) and all(r["acceptance_met"] for r in results)
        summary = (
            "\n".join(
                f"{r['probe']}: head={r['head_exit_code']} base={r['base_exit_code']} -> "
                f"{'PASS' if r['acceptance_met'] else 'FAIL'}"
                for r in results
            )
            or "no probes selected"
        )
        payload = {
            "status": "ok",
            "head_sha": args.head,
            "base_sha": args.base or None,
            "acceptance_met": met,
            "probe_command": (
                f"verify/run_all.sh --head {args.head} --base {args.base} "
                f"--issues {args.issues or '-'} --regression {args.regression or '-'}"
                + (f" --requirements {args.requirements}" if args.requirements else "")
            ),
            "probe_exit_code": 0 if met else 1,
            "evidence": summary,
            "results": results,
        }
        if args.health_url:
            payload["superset_health_url"] = args.health_url

    errors = validate(VERIFICATION_SCHEMA, payload)
    if errors:
        sys.stderr.write("result does not match VERIFICATION_SCHEMA:\n  " + "\n  ".join(errors) + "\n")
        return 3
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0 if payload["status"] == "ok" and payload["acceptance_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
