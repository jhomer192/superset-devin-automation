"""Turn probe exit codes into the verification verdict (orchestrator.schema.VERIFICATION_SCHEMA).

Rules, applied mechanically:
  * a probe of a PRD requirement in --requirements is accepted iff it exited 0 at HEAD
  * a probe that belongs to none of the named requirements is never accepted
  * acceptance_met is the AND over every probe; no probes at all => acceptance_met is false
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
    requirements: set[str],
    registry: Registry,
) -> list[dict[str, Any]]:
    guarded_by: dict[str, list[str]] = {}
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
        head_code = int(head["exit_code"]) if head else 255
        reqs = guarded_by.get(probe_id, [])
        met = bool(reqs) and head_code == 0
        evidence = f"HEAD exit {head_code}:\n{_tail(head['log']) if head else '<not run>'}"
        results.append(
            {
                "issue": int(entry["issue"]),
                "probe": probe_id,
                "kind": entry["kind"],
                "head_exit_code": head_code,
                "acceptance_met": met,
                "evidence": evidence,
                "requirements": reqs,
            }
        )
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes")
    ap.add_argument("--requirements", default="")
    ap.add_argument("--head", required=True)
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
            "error_message": f"stage {args.stage} failed: {args.message}",
        }
    else:
        rows = [json.loads(line) for line in Path(args.probes).read_text().splitlines() if line.strip()]
        requirements = {x.strip() for x in args.requirements.split(",") if x.strip()}
        results = build_results(rows, requirements, load_registry())
        met = bool(results) and all(r["acceptance_met"] for r in results)
        summary = (
            "\n".join(
                f"{r['probe']} [{', '.join(r['requirements']) or '-'}]: exit {r['head_exit_code']} -> "
                f"{'PASS' if r['acceptance_met'] else 'FAIL'}"
                for r in results
            )
            or "no probes selected"
        )
        payload = {
            "status": "ok",
            "head_sha": args.head,
            "acceptance_met": met,
            "probe_command": (
                f"verify/run_all.sh --head {args.head} --requirements {args.requirements or '-'}"
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
