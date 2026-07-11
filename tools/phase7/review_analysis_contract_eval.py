from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


_QUALITY_DIMENSIONS = (
    "directness",
    "insight",
    "actionability",
    "evidence_discipline",
)
_RUNTIME_DIMENSIONS = (
    "all_required_queries_complete",
    "all_capabilities_bound",
    "all_claims_traceable",
)


def review_artifact(
    path: str | Path,
    *,
    baseline: str | Path | None = None,
) -> dict[str, Any]:
    artifact_path = Path(path).resolve()
    review = _review_payload(_load(artifact_path), artifact_path=artifact_path)
    if baseline is not None:
        baseline_path = Path(baseline).resolve()
        baseline_review = _review_payload(
            _load(baseline_path),
            artifact_path=baseline_path,
        )
        review["baseline_comparison"] = {
            "answer_quality_delta": {
                key: round(
                    review["answer_quality"][key]
                    - baseline_review["answer_quality"][key],
                    2,
                )
                for key in _QUALITY_DIMENSIONS
            },
            "runtime_regressions": [
                key
                for key in _RUNTIME_DIMENSIONS
                if baseline_review["runtime_correctness"][key]
                and not review["runtime_correctness"][key]
            ],
        }
    return review


def _load(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("analysis_eval_artifact_invalid")
    return payload


def _review_payload(
    payload: Mapping[str, Any],
    *,
    artifact_path: Path,
) -> dict[str, Any]:
    turn_reviews = [
        _review_turn(
            turn,
            internal_audit=_load_internal_final_audit(turn, artifact_path),
        )
        for turn in payload.get("turns") or ()
        if isinstance(turn, Mapping)
    ]
    runtime = {
        key: bool(turn_reviews)
        and all(item["runtime_correctness"][key] for item in turn_reviews)
        for key in _RUNTIME_DIMENSIONS
    }
    aggregate_runtime = (
        (payload.get("real_clickhouse_review") or {}).get("runtime_correctness")
        or {}
    )
    for key in _RUNTIME_DIMENSIONS:
        if key in aggregate_runtime:
            runtime[key] = aggregate_runtime[key] is True
    quality = {
        key: round(
            sum(item["answer_quality"][key] for item in turn_reviews)
            / len(turn_reviews),
            2,
        )
        if turn_reviews
        else 1
        for key in _QUALITY_DIMENSIONS
    }
    quality["risk_markers"] = sorted(
        {
            marker
            for item in turn_reviews
            for marker in item["answer_quality"]["risk_markers"]
        }
    )
    supplied_coverage = payload.get("coverage_summary") or {}
    obligation = supplied_coverage.get("obligation_coverage") or {
        "required": 0,
        "executed": 0,
        "degraded": 0,
        "missing": 0,
    }
    return {
        "case_id": str(payload.get("case_id") or ""),
        "obligation_coverage": obligation,
        "dataset_coverage": supplied_coverage.get("dataset_coverage") or {},
        "runtime_correctness": runtime,
        "hard_acceptance": supplied_coverage.get("hard_acceptance") or {
            "runtime_passed": all(runtime.values()),
            "obligation_passed": False,
            "passed": False,
        },
        "answer_quality": quality,
        "quality_scores_block_display": False,
        "final_answer_audit_coverage": {
            "available": sum(
                item["final_answer_audit_status"] == "available"
                for item in turn_reviews
            ),
            "unavailable": sum(
                item["final_answer_audit_status"] != "available"
                for item in turn_reviews
            ),
        },
        "clarification_resume": supplied_coverage.get("clarification_resume") or {
            "required": 0,
            "resumed": 0,
        },
        "reuse_coverage": supplied_coverage.get("reuse_coverage") or {
            "required": 0,
            "same_topic": 0,
        },
        "turns": turn_reviews,
    }


def _review_turn(
    turn: Mapping[str, Any],
    *,
    internal_audit: tuple[Mapping[str, Any] | None, str],
) -> dict[str, Any]:
    runtime_review = turn.get("real_clickhouse_review") or {}
    runtime = runtime_review.get("runtime_correctness") or {}
    correctness = {key: runtime.get(key) is True for key in _RUNTIME_DIMENSIONS}
    package, audit_status = internal_audit
    if package is None:
        return {
            "index": turn.get("index"),
            "runtime_correctness": correctness,
            "runtime_issues": list(runtime_review.get("issues") or ()),
            "final_answer_audit_status": audit_status,
            "answer_quality": {
                **{key: 1 for key in _QUALITY_DIMENSIONS},
                "risk_markers": ["final_answer_audit_unavailable"],
            },
            "quality_scores_block_display": False,
        }
    quality = package.get("quality_gate") or {}
    warnings = {
        str(item)
        for item in (
            *(quality.get("repairable_warnings") or ()),
            *(quality.get("issues") or ()),
        )
        if item
    }
    risk_markers = {
        str(item)
        for item in (
            *(quality.get("risk_markers") or ()),
            *(quality.get("risk_flags") or ()),
            *((package.get("quality_gate") or {}).get("risk_flags") or ()),
            *warnings,
        )
        if item
    }
    has_answer = bool(str(package.get("final_answer") or "").strip())
    directness = 5 if quality.get("direct_answer") is True else (3 if has_answer else 1)
    if "missing_wording_anchor" in warnings:
        directness = max(1, directness - 1)
    insight = 5 if quality.get("business_insight_present") is True else (3 if has_answer else 1)
    if warnings & {"missing_business_interpretation", "weak_business_interpretation"}:
        insight = min(insight, 2)
    actionability = 5 if quality.get("followups_one_intent") is True else (3 if has_answer else 1)
    if "weak_followup" in warnings:
        actionability = min(actionability, 2)
    runtime_passed = all(correctness.values())
    if runtime_passed and quality.get("has_verified_claims") is True:
        evidence_discipline = (
            5 if quality.get("verified_claim_preserved") is True else 4
        )
    elif runtime_passed:
        evidence_discipline = 3
    else:
        evidence_discipline = 1
    return {
        "index": turn.get("index"),
        "runtime_correctness": correctness,
        "runtime_issues": list(runtime_review.get("issues") or ()),
        "final_answer_audit_status": audit_status,
        "answer_quality": {
            "directness": directness,
            "insight": insight,
            "actionability": actionability,
            "evidence_discipline": evidence_discipline,
            "risk_markers": sorted(risk_markers),
        },
        "quality_scores_block_display": False,
    }


def _load_internal_final_audit(
    turn: Mapping[str, Any],
    eval_artifact_path: Path,
) -> tuple[Mapping[str, Any] | None, str]:
    resumed = bool(turn.get("resumed_status"))
    client_package = (
        turn.get("resumed_answer_package") if resumed else turn.get("answer_package")
    )
    if not isinstance(client_package, Mapping):
        client_package = {}
    raw_path = (
        (turn.get("resumed_artifact_path") if resumed else turn.get("artifact_path"))
        or client_package.get("artifact_path")
    )
    expected_run_id = str(
        (turn.get("resumed_run_id") if resumed else turn.get("run_id")) or ""
    )
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "artifact_path_missing"
    artifact_root = _artifact_root(eval_artifact_path)
    if artifact_root is None:
        return None, "artifact_root_unavailable"
    internal_path = Path(raw_path)
    if not internal_path.is_absolute():
        internal_path = artifact_root.parent / internal_path
    try:
        internal_path = internal_path.resolve()
        internal_path.relative_to(artifact_root)
    except (OSError, ValueError):
        return None, "artifact_path_outside_root"
    try:
        payload = _load(internal_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None, "artifact_unavailable"
    if not expected_run_id or str(payload.get("run_id") or "") != expected_run_id:
        return None, "run_id_mismatch"
    quality_gate = payload.get("quality_gate")
    if not isinstance(quality_gate, Mapping):
        return None, "quality_gate_missing"
    required_quality_fields = {
        "direct_answer",
        "business_insight_present",
        "followups_one_intent",
        "has_verified_claims",
        "verified_claim_preserved",
    }
    if not required_quality_fields.issubset(quality_gate):
        return None, "quality_gate_incomplete"
    audit_calls = tuple(
        item
        for item in payload.get("llm_calls") or ()
        if isinstance(item, Mapping)
        and item.get("task") == "final_answer_audit"
        and isinstance(item.get("structured_output"), Mapping)
    )
    if not audit_calls:
        return None, "final_llm_audit_missing"
    return payload, "available"


def _artifact_root(path: Path) -> Path | None:
    for candidate in (path.parent, *path.parents):
        if candidate.name == "artifacts":
            return candidate.resolve()
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact")
    parser.add_argument("--baseline")
    parser.add_argument("--out")
    args = parser.parse_args()
    review = review_artifact(args.artifact, baseline=args.baseline)
    rendered = json.dumps(review, ensure_ascii=False, indent=2)
    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
