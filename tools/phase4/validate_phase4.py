#!/usr/bin/env python3
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime, ENV_NAMES
from bi_agent.runtime.langgraph_workflow import run_pattern_workflow
from bi_agent.runtime.sql_safety import validate_select_only


CASE_FILE = ROOT / "evals" / "phase4" / "pattern_cases.yaml"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "phase-4"
REAL_SQL_ENV = "WAJE_PHASE4_PATTERN_SQL"
REPAIR_PATH = "provide read-only ClickHouse env vars and accepted physical binding"


@dataclass(frozen=True)
class EvalCaseResult:
    case_id: str
    pattern_family: str
    status: str
    reason: str
    artifact_path: str = ""
    non_real_data: bool = True
    owner: str = ""
    repair_path: str = ""
    business_conclusion_published: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SiblingSummary:
    passed_count: int
    degraded_or_blocked: tuple[EvalCaseResult, ...]


@dataclass(frozen=True)
class FixtureEvalResult:
    engineering_fixture_passed: bool
    month_start_case: EvalCaseResult
    sibling_summary: SiblingSummary
    cases: tuple[EvalCaseResult, ...]


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    ok: bool
    returncode: int
    output_tail: str


@dataclass(frozen=True)
class Phase4ValidationResult:
    ok: bool
    command_results: tuple[CommandResult, ...]
    fixture_eval: FixtureEvalResult
    real_month_start: EvalCaseResult


def load_cases(path: Path = CASE_FILE) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return list(loaded.get("cases", ()))


def run_fixture_eval(
    *,
    artifact_root: Optional[str] = None,
    case_file: Path = CASE_FILE,
) -> FixtureEvalResult:
    cases = [
        run_eval_case(case, mode="fixture", artifact_root=artifact_root)
        for case in load_cases(case_file)
    ]
    month_start = _case_by_id(cases, "month_start")
    siblings = tuple(case for case in cases if case.case_id != "month_start")
    degraded_or_blocked = tuple(
        case for case in siblings if case.status in {"degraded", "blocked", "failed"}
    )
    fixture_passed = all(case.status == "passed" for case in cases)
    return FixtureEvalResult(
        engineering_fixture_passed=fixture_passed,
        month_start_case=month_start,
        sibling_summary=SiblingSummary(
            passed_count=sum(1 for case in siblings if case.status == "passed"),
            degraded_or_blocked=degraded_or_blocked,
        ),
        cases=tuple(cases),
    )


def run_real_eval(
    *,
    artifact_root: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    case_id: str = "month_start",
    case_file: Path = CASE_FILE,
) -> EvalCaseResult:
    env = dict(os.environ if environ is None else environ)
    missing_env = tuple(name for name in ENV_NAMES if not env.get(name))
    case = _find_case(case_id, load_cases(case_file))
    if missing_env:
        return _external_blocked_case(
            case,
            reason="external_dependency_blocked",
            detail=f"missing ClickHouse env: {', '.join(missing_env)}",
        )

    sql = env.get(REAL_SQL_ENV, "")
    if not sql:
        return _external_blocked_case(
            case,
            reason="external_dependency_blocked",
            detail=f"missing accepted physical binding: {REAL_SQL_ENV}",
        )

    validation = validate_select_only(sql, aggregate=True)
    if not validation.ok:
        return _external_blocked_case(
            case,
            reason="external_dependency_blocked",
            detail=f"physical binding SQL rejected: {validation.reason}",
            diagnostics={
                "validator_results": (
                    {
                        "validator": "sql_safety",
                        "ok": False,
                        "reason": validation.reason,
                        "sql_hash": validation.query_hash,
                    },
                )
            },
        )

    with _patched_environ(env):
        runtime = ClickHouseRuntime.from_env()
        if not runtime.configured():
            return _external_blocked_case(
                case,
                reason="external_dependency_blocked",
                detail=f"invalid ClickHouse binding: {runtime.binding.reason}",
                diagnostics={
                    "query_hash": validation.query_hash,
                    "validator_results": (
                        {
                            "validator": "sql_safety",
                            "ok": validation.ok,
                            "reason": validation.reason,
                            "sql_hash": validation.query_hash,
                        },
                        {
                            "validator": "runtime_binding",
                            "ok": False,
                            "reason": runtime.binding.reason,
                        },
                    ),
                },
            )
        query_result = runtime.aggregate(sql, query_id=f"phase4-{case_id}")

    if not query_result.ok:
        return EvalCaseResult(
            case_id=case["case_id"],
            pattern_family=case["pattern_family"],
            status="failed",
            reason=query_result.reason or "clickhouse_query_failed",
            non_real_data=False,
            owner="data_engineering_owner",
            repair_path="inspect ClickHouse query failure and accepted physical binding",
            business_conclusion_published=False,
            diagnostics={
                "query_error": query_result.reason,
                "query_hash": query_result.query_hash or validation.query_hash,
                "validator_results": (
                    {
                        "validator": "sql_safety",
                        "ok": validation.ok,
                        "reason": validation.reason,
                        "sql_hash": validation.query_hash,
                    },
                ),
            },
        )

    real_case = dict(case)
    real_case["fixture_rows"] = list(query_result.rows)
    return run_eval_case(
        real_case,
        mode="real",
        artifact_root=artifact_root,
        sql_text=sql,
    )


def run_eval_case(
    case: Mapping[str, Any],
    *,
    mode: str,
    artifact_root: Optional[str] = None,
    sql_text: str = "",
) -> EvalCaseResult:
    run_id = f"phase4-{mode}-{case['case_id']}"
    request = {
        "run_id": run_id,
        "artifact_root": artifact_root or str(DEFAULT_ARTIFACT_ROOT),
        "pattern_family": case["pattern_family"],
        "pattern_params": dict(case.get("pattern_params", {})),
        "time_window": case.get("time_window", "2024-01..2026-05"),
        "rows": list(case.get("fixture_rows", ())),
        "required_fields": _required_fields_for_case(case),
        "requested_nodes": tuple(case.get("required_capabilities", ())),
    }
    if sql_text:
        request["sql_text"] = sql_text

    result = run_pattern_workflow(request)
    non_real_data = mode == "fixture"
    if result.status != "draft" or result.answer_package is None:
        return EvalCaseResult(
            case_id=case["case_id"],
            pattern_family=case["pattern_family"],
            status="failed",
            reason=result.failure_reason or "workflow_failed",
            artifact_path=result.artifact_path,
            non_real_data=non_real_data,
            business_conclusion_published=False,
        )

    _mark_artifact(
        result.artifact_path,
        {
            "eval_case_id": case["case_id"],
            "eval_mode": mode,
            "non_real_data": non_real_data,
        },
    )
    status, reason = _status_from_answer_package(
        result.answer_package, case["pattern_family"]
    )
    return EvalCaseResult(
        case_id=case["case_id"],
        pattern_family=case["pattern_family"],
        status=status,
        reason=reason,
        artifact_path=result.artifact_path,
        non_real_data=non_real_data,
        business_conclusion_published=mode == "real" and status == "passed",
    )


def run_validation_suite(*, run_commands: bool = True) -> Phase4ValidationResult:
    command_results = (
        tuple(_run_validation_commands()) if run_commands else tuple()
    )
    fixture_eval = run_fixture_eval()
    real_month_start = run_real_eval()
    ok = (
        all(result.ok for result in command_results)
        and fixture_eval.engineering_fixture_passed
        and real_month_start.status in {"passed", "blocked"}
    )
    return Phase4ValidationResult(
        ok=ok,
        command_results=command_results,
        fixture_eval=fixture_eval,
        real_month_start=real_month_start,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ = argv
    result = run_validation_suite()
    print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1


def _run_validation_commands() -> list[CommandResult]:
    commands = [
        ("python3", "-m", "unittest", "discover", "-s", "tests/phase4"),
        ("ruby", "tools/evals/validate-phase-3.rb"),
        ("git", "diff", "--check"),
    ]
    results = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        results.append(
            CommandResult(
                command=command,
                ok=completed.returncode == 0,
                returncode=completed.returncode,
                output_tail=output[-4000:],
            )
        )
    return results


def _status_from_answer_package(
    package: Mapping[str, Any], pattern_family: str
) -> tuple[str, str]:
    evidence = _evidence_items(package)
    pattern = next(
        (
            item
            for item in evidence
            if item.get("capability") == "pattern_scan"
            and item.get("pattern_family") == pattern_family
        ),
        None,
    )
    data_quality = next(
        (item for item in evidence if item.get("capability") == "data_quality_check"),
        None,
    )
    if data_quality and data_quality.get("limitations"):
        return "degraded", ",".join(data_quality["limitations"])
    if pattern is None:
        return "failed", "missing_pattern_scan_evidence"
    if pattern.get("established"):
        return "passed", "pattern_established"
    limitations = tuple(pattern.get("limitations", ()))
    return "degraded", ",".join(limitations) or "pattern_not_established"


def _evidence_items(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    for section in package.get("sections", ()):
        if section.get("section_id") == "evidence":
            return list(section.get("payload", {}).get("evidence", ()))
    return []


def _required_fields_for_case(case: Mapping[str, Any]) -> tuple[str, ...]:
    if case.get("required_fields"):
        return tuple(case["required_fields"])

    params = dict(case.get("pattern_params", {}))
    pattern_family = case["pattern_family"]
    fields = ["amount"]
    if pattern_family == "intra_period":
        fields.extend(
            [
                params.get("period_key", "month"),
                params.get("group_key", "phase"),
            ]
        )
    elif pattern_family == "weekly":
        fields.extend(
            [
                params.get("week_key", "week"),
                params.get("weekday_key", "weekday"),
            ]
        )
    elif pattern_family == "event_relative":
        fields.extend(
            [
                params.get("event_key", "event_id"),
                params.get("window_key", "window"),
            ]
        )
    elif pattern_family == "rolling":
        fields.append(params.get("period_key", "window"))
        if any("baseline_high" in row for row in case.get("fixture_rows", ())):
            fields.append("baseline_high")
        else:
            fields.append(params.get("group_key", "group"))
    elif pattern_family == "custom_baseline":
        fields.extend(
            [
                params.get("period_key", "period"),
                params.get("group_key", "group"),
            ]
        )
    return tuple(dict.fromkeys(fields))


def _external_blocked_case(
    case: Mapping[str, Any],
    *,
    reason: str,
    detail: str,
    diagnostics: Optional[Mapping[str, Any]] = None,
) -> EvalCaseResult:
    return EvalCaseResult(
        case_id=case["case_id"],
        pattern_family=case["pattern_family"],
        status="blocked",
        reason=reason,
        non_real_data=False,
        owner="data_engineering_owner",
        repair_path=f"{REPAIR_PATH}; {detail}",
        business_conclusion_published=False,
        diagnostics=dict(diagnostics or {}),
    )


def _mark_artifact(path: str, metadata: Mapping[str, Any]) -> None:
    if not path:
        return
    artifact_path = Path(path)
    with artifact_path.open(encoding="utf-8") as handle:
        artifact = json.load(handle)
    artifact.update(metadata)
    with artifact_path.open("w", encoding="utf-8") as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _case_by_id(cases: Sequence[EvalCaseResult], case_id: str) -> EvalCaseResult:
    for case in cases:
        if case.case_id == case_id:
            return case
    raise ValueError(f"missing eval case: {case_id}")


def _find_case(case_id: str, cases: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for case in cases:
        if case.get("case_id") == case_id:
            return case
    raise ValueError(f"missing eval case: {case_id}")


@contextmanager
def _patched_environ(environ: Mapping[str, str]):
    original = os.environ.copy()
    os.environ.clear()
    os.environ.update({key: str(value) for key, value in environ.items()})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
