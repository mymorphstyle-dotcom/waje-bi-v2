#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.phase4.validate_phase4 import load_cases, run_eval_case, run_real_eval


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="month_start")
    parser.add_argument("--mode", choices=("fixture", "real"), required=True)
    parser.add_argument("--artifact-root", default=str(ROOT / "artifacts" / "phase-4"))
    args = parser.parse_args()

    if args.mode == "real":
        result = run_real_eval(
            artifact_root=args.artifact_root,
            case_id=args.case,
        )
    else:
        case = _find_case(args.case)
        result = run_eval_case(
            case,
            mode="fixture",
            artifact_root=args.artifact_root,
        )

    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code_for_result(args.mode, result.status)


def exit_code_for_result(mode: str, status: str) -> int:
    if mode == "fixture":
        return 0 if status == "passed" else 1
    return 0 if status in {"passed", "degraded", "blocked"} else 1


def _find_case(case_id: str) -> dict:
    for case in load_cases():
        if case.get("case_id") == case_id:
            return case
    raise SystemExit(f"unknown case: {case_id}")


if __name__ == "__main__":
    raise SystemExit(main())
