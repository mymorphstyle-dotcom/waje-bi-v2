#!/usr/bin/env python3
"""Fail closed unless the canonical Gate 3 E0 authority allows G3.1."""

from __future__ import annotations

import json

from gate3_admission_authority import AdmissionAuthorityConnector
from verify_gate3_e0 import compute_readiness


def main(
    admission_connector: AdmissionAuthorityConnector | None = None,
) -> int:
    readiness, findings = compute_readiness(
        admission_connector=admission_connector
    )
    allowed = (
        not findings
        and readiness["derived_status"] == "ready"
        and readiness["entry_decision"] == "allow_g3_1"
    )
    print(
        json.dumps(
            {
                "status": "passed" if allowed else "blocked",
                "entry_decision": readiness["entry_decision"],
                "findings": findings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
