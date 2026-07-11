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
REAL_2026H1_CASE_FILE = ROOT / "evals" / "phase4" / "real_2026h1_pattern_cases.yaml"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "phase-4"
REAL_SQL_ENV = "WAJE_PHASE4_PATTERN_SQL"
REPAIR_PATH = "provide read-only ClickHouse env vars and accepted physical binding"
PRIMARY_PATTERN_EVIDENCE_CAPABILITIES = frozenset(
    {
        "pattern_scan",
        "compare_period_phases",
        "compare_periods",
        "rolling_window_compare",
        "weekday_calendar_compare",
        "event_window_compare",
    }
)


def classify_route_drift(
    *,
    pattern_family: str,
    accepted_graph: Sequence[str],
    primary_evidence_capability: str,
    expected_primary_capabilities: Sequence[str],
    eval_status: str,
) -> dict[str, str | bool]:
    observed = primary_evidence_capability not in set(expected_primary_capabilities)
    impact = "none"
    if observed:
        impact = "conclusion" if eval_status in {"failed", "blocked"} else "evidence_shape"
    return {
        "route_drift_observed": observed,
        "route_drift_impact": impact,
        "guardrail_promotion": "requires_human_review"
        if observed
        else "not_applicable",
    }


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
    real_2026h1_eval: "RealEvalSuiteResult"


@dataclass(frozen=True)
class RealEvalSuiteResult:
    passed: bool
    cases: tuple[EvalCaseResult, ...]
    mismatches: tuple[dict[str, str], ...]


def load_cases(path: Path = CASE_FILE) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return list(loaded.get("cases", ()))


def run_fixture_eval(
    *,
    artifact_root: Optional[str] = None,
    case_file: Path = CASE_FILE,
    llm_client: Any = None,
) -> FixtureEvalResult:
    cases = [
        run_eval_case(
            case,
            mode="fixture",
            artifact_root=artifact_root,
            llm_client=llm_client,
        )
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
    llm_client: Any = None,
) -> EvalCaseResult:
    env = (
        {**_load_local_env(ROOT / ".env"), **os.environ}
        if environ is None
        else dict(environ)
    )
    missing_env = tuple(name for name in ENV_NAMES if not env.get(name))
    case = _find_case(case_id, load_cases(case_file))
    if missing_env:
        return _external_blocked_case(
            case,
            reason="external_dependency_blocked",
            detail=f"missing ClickHouse env: {', '.join(missing_env)}",
        )

    sql = case.get("real_sql") or env.get(REAL_SQL_ENV, "")
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
            llm_client=llm_client,
        )


def run_real_2026h1_eval(
    *,
    artifact_root: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    case_file: Path = REAL_2026H1_CASE_FILE,
    llm_client: Any = None,
) -> RealEvalSuiteResult:
    cases = load_cases(case_file)
    results = tuple(
        run_real_eval(
            artifact_root=artifact_root,
            environ=environ,
            case_id=case["case_id"],
            case_file=case_file,
            llm_client=llm_client,
        )
        for case in cases
    )
    expected = {case["case_id"]: case.get("expected_status") for case in cases}
    mismatches = tuple(
        {
            "case_id": result.case_id,
            "expected": str(expected[result.case_id]),
            "actual": result.status,
        }
        for result in results
        if expected.get(result.case_id) and result.status != expected[result.case_id]
    )
    return RealEvalSuiteResult(
        passed=not mismatches,
        cases=results,
        mismatches=mismatches,
    )


def run_eval_case(
    case: Mapping[str, Any],
    *,
    mode: str,
    artifact_root: Optional[str] = None,
    sql_text: str = "",
    llm_client: Any = None,
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
        "allow_question_interrupt": False,
        # This legacy node-level harness materializes rows before the workflow.
        # It remains a diagnostic fixture; production/e2e validation enters via Core.
        "run_mode": "fixture",
        "source_mode": mode,
    }
    for key in ("question", "baseline", "target"):
        if key in case:
            request[key] = case[key]
    if sql_text:
        request["sql_text"] = sql_text
    if llm_client is not None:
        request["llm_client"] = llm_client

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
    if not _has_required_llm_audit(result.answer_package):
        return EvalCaseResult(
            case_id=case["case_id"],
            pattern_family=case["pattern_family"],
            status="failed",
            reason="missing_required_llm_audit",
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
    env = {**_load_local_env(ROOT / ".env"), **os.environ}
    with _patched_environ(env):
        fixture_eval = run_fixture_eval()
        real_month_start = run_real_eval()
        real_2026h1_eval = run_real_2026h1_eval()
    ok = (
        all(result.ok for result in command_results)
        and fixture_eval.engineering_fixture_passed
        and real_month_start.status in {"passed", "blocked"}
        and real_2026h1_eval.passed
    )
    return Phase4ValidationResult(
        ok=ok,
        command_results=command_results,
        fixture_eval=fixture_eval,
        real_month_start=real_month_start,
        real_2026h1_eval=real_2026h1_eval,
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
    final = package.get("final_explanation", {})
    evidence = _evidence_items(package)
    pattern = next(
        (
            item
            for item in evidence
            if _evidence_capability(item) in PRIMARY_PATTERN_EVIDENCE_CAPABILITIES
            and _evidence_pattern_family(item) == pattern_family
        ),
        None,
    )
    data_quality = next(
        (
            item
            for item in evidence
            if _evidence_capability(item)
            in {"data_quality_check", "data_quality_profile"}
        ),
        None,
    )
    if data_quality and _evidence_limitations(data_quality):
        return "degraded", ",".join(_evidence_limitations(data_quality))
    if pattern is None:
        return "failed", "missing_primary_pattern_evidence"
    limitation_reason = ",".join(_evidence_limitations(pattern))
    evidence_verifier_block = final.get("code") == "evidence_verifier_failed"
    if final.get("status") == "blocked" and not evidence_verifier_block:
        return "blocked", final.get("explanation") or limitation_reason or "blocked"
    if final.get("status") == "degraded" and not evidence_verifier_block:
        return "degraded", ",".join(
            item
            for item in (limitation_reason, final.get("explanation", "degraded"))
            if item
        )
    if _evidence_established_for_eval(pattern):
        return "passed", "pattern_established"
    limitations = _evidence_limitations(pattern)
    return "degraded", ",".join(limitations) or "pattern_not_established"


def _evidence_capability(item: Mapping[str, Any]) -> str:
    return str(item.get("capability_id") or item.get("capability") or "")


def _evidence_pattern_family(item: Mapping[str, Any]) -> str:
    payload = item.get("typed_payload", {})
    return str(item.get("pattern_family") or payload.get("pattern_family") or "")


def _evidence_limitations(item: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(value) for value in item.get("limitations", ()) if value)


def _evidence_established_for_eval(item: Mapping[str, Any]) -> bool:
    if "established" in item:
        return bool(item.get("established"))
    payload = item.get("typed_payload", {})
    if "established" in payload:
        return bool(payload.get("established"))
    if _evidence_limitations(item):
        return False
    return item.get("wording_limit") == "supported" and item.get("strength") in {
        "high",
        "medium",
    }


def _has_required_llm_audit(package: Mapping[str, Any]) -> bool:
    calls = package.get("admin_audit", {}).get("llm_calls", ())
    if any(
        not call.get("messages")
        or "required_keys" not in call
        or "raw_response_content" not in call
        or "started_at" not in call
        or "finished_at" not in call
        or "duration_ms" not in call
        for call in calls
    ):
        return False
    seen = {call.get("task") for call in calls}
    common = {
        "business_intent",
        "boundary_decision",
        "confirm_understanding",
        "analysis_route",
        "data_coverage_interpretation",
        "next_action",
    }
    answer_path = {
        "evidence_interpretation",
        "answer_synthesis",
        "semantic_audit",
        "final_business_summary",
    }
    terminal_path = {
        "degraded_explanation",
        "blocked_explanation",
        "final_business_summary",
    }
    return common.issubset(seen) and (
        answer_path.issubset(seen) or bool(terminal_path & seen)
    )


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


def _load_local_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


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
