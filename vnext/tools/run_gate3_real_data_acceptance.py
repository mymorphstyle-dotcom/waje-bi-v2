#!/usr/bin/env python3
"""Run agent-supplied period designs against the accepted local snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import date, datetime, UTC
from pathlib import Path

from waje_vnext.capabilities import (
    OrdinalGroupSpec,
    PeriodComparisonQuerySpec,
    PeriodUnit,
    SourceBinding,
    compile_period_comparison_sql,
    parse_period_comparison_tsv,
    summarize_period_comparison,
)
from waje_vnext.domain.authority import ComparisonGroupRole
from waje_vnext.domain.canonical import (
    content_sha256,
    freeze_json,
    to_jsonable,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="waje-bi-clickhouse")
    parser.add_argument(
        "--artifact-root",
        default="artifacts/gate3-real-data",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source_contract = json.loads(
        (
            root
            / "contracts"
            / "semantics"
            / "source-paid-order-daily.v1.json"
        ).read_text(encoding="utf-8")
    )
    binding = SourceBinding(
        source_ref=source_contract["contract_id"],
        metric_ref=source_contract["metric_ref"],
        table=source_contract["physical_table"],
        date_column=source_contract["date_column"],
        value_column=source_contract["value_column"],
        snapshot_release_ref=source_contract["snapshot_release_ref"],
        business_timezone=source_contract["business_timezone"],
        available_from=date.fromisoformat(source_contract["available_from"]),
        available_through=date.fromisoformat(
            source_contract["available_through"]
        ),
    )
    designs = (
        PeriodComparisonQuerySpec(
            query_spec_id="query-spec:gate3-agent-candidate-a:v1",
            metric_ref=binding.metric_ref,
            source_ref=binding.source_ref,
            period_unit=PeriodUnit.CALENDAR_MONTH,
            range_start=date(2024, 1, 1),
            range_end=date(2026, 5, 31),
            groups=(
                OrdinalGroupSpec(
                    group_id="phase-a",
                    role=ComparisonGroupRole.FOCAL,
                    lower_inclusive=1,
                    upper_inclusive=10,
                ),
                OrdinalGroupSpec(
                    group_id="phase-b",
                    role=ComparisonGroupRole.REFERENCE,
                    lower_inclusive=11,
                    upper_inclusive=20,
                ),
                OrdinalGroupSpec(
                    group_id="phase-c",
                    role=ComparisonGroupRole.REFERENCE,
                    lower_inclusive=21,
                    upper_inclusive=31,
                ),
            ),
        ),
        PeriodComparisonQuerySpec(
            query_spec_id="query-spec:gate3-agent-candidate-b:v1",
            metric_ref=binding.metric_ref,
            source_ref=binding.source_ref,
            period_unit=PeriodUnit.CALENDAR_MONTH,
            range_start=date(2024, 1, 1),
            range_end=date(2026, 5, 31),
            groups=(
                OrdinalGroupSpec(
                    group_id="phase-a",
                    role=ComparisonGroupRole.FOCAL,
                    lower_inclusive=1,
                    upper_inclusive=5,
                ),
                OrdinalGroupSpec(
                    group_id="phase-b",
                    role=ComparisonGroupRole.REFERENCE,
                    lower_inclusive=6,
                    upper_inclusive=24,
                ),
                OrdinalGroupSpec(
                    group_id="phase-c",
                    role=ComparisonGroupRole.REFERENCE,
                    lower_inclusive=25,
                    upper_inclusive=31,
                ),
            ),
        ),
    )
    results = []
    for spec in designs:
        sql = compile_period_comparison_sql(spec, binding)
        completed = subprocess.run(
            [
                "docker",
                "exec",
                args.container,
                "clickhouse-client",
                "--query",
                sql,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = parse_period_comparison_tsv(completed.stdout)
        summary = summarize_period_comparison(spec, rows)
        results.append(
            {
                "query_spec": to_jsonable(spec),
                "query_spec_sha256": spec.content_sha256,
                "compiled_query_sha256": hashlib.sha256(
                    sql.encode("utf-8")
                ).hexdigest(),
                "row_count": len(rows),
                "summary": summary,
            }
        )
    artifact = {
        "acceptance": "gate3-real-data-period-comparison",
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "source_contract": source_contract,
        "designs": results,
    }
    artifact["content_sha256"] = content_sha256(freeze_json(artifact))
    artifact_root = root / args.artifact_root
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_root / "period-comparison.json"
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "artifact": str(artifact_path),
                "content_sha256": artifact["content_sha256"],
                "design_count": len(results),
                "comparable_periods": [
                    result["summary"]["comparable_periods"]
                    for result in results
                ],
                "contrasts": [
                    result["summary"]["contrasts"]
                    for result in results
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
