from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.runtime.insight_governance_persistence import (  # noqa: E402
    load_insight_review_publication,
    persist_insight_quality_evaluation,
)
from bi_agent.runtime.evidence_authority import canonical_digest  # noqa: E402
from bi_agent.runtime.insight_quality_rubric import (  # noqa: E402
    INSIGHT_QUALITY_DIMENSIONS,
    InsightEvaluationCaseSnapshot,
    InsightQualityRubric,
)
from bi_agent.runtime.publication_authority import (  # noqa: E402
    InsightQualityEvaluation,
    NarrativeAttemptRequest,
)
from tools.phase7 import run_live_conversation_system_test as live_acceptance  # noqa: E402


def _score(value: str) -> int:
    score = int(value)
    if not 1 <= score <= 5:
        raise argparse.ArgumentTypeError("score must be between 1 and 5")
    return score


def _human_reason(value: str) -> str:
    if not value or value != value.strip():
        raise argparse.ArgumentTypeError("human reason must be non-empty")
    return value


def _connect_runtime_database() -> Any:
    database_url = os.environ.get("WAJE_RUNTIME_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not database_url:
        raise RuntimeError("runtime_database_url_required")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("psycopg_required") from exc
    return psycopg.connect(database_url, row_factory=dict_row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Persist one human insight-quality review of an immutable publication."
        )
    )
    parser.add_argument("--acceptance-summary", required=True, type=Path)
    parser.add_argument("--reviewer-ref", required=True)
    parser.add_argument("--explanation-value", required=True, type=_score)
    parser.add_argument("--explanation-value-reason", required=True, type=_human_reason)
    parser.add_argument("--novelty", required=True, type=_score)
    parser.add_argument("--novelty-reason", required=True, type=_human_reason)
    parser.add_argument("--decision-usefulness", required=True, type=_score)
    parser.add_argument(
        "--decision-usefulness-reason", required=True, type=_human_reason
    )
    parser.add_argument("--competing-hypotheses", required=True, type=_score)
    parser.add_argument(
        "--competing-hypotheses-reason", required=True, type=_human_reason
    )
    parser.add_argument("--uncertainty-handling", required=True, type=_score)
    parser.add_argument(
        "--uncertainty-handling-reason", required=True, type=_human_reason
    )
    parser.add_argument("--actionability", required=True, type=_score)
    parser.add_argument("--actionability-reason", required=True, type=_human_reason)
    parser.add_argument("--reviewed-at")
    parser.add_argument("--requested-attempt-id")
    parser.add_argument(
        "--reason-dimension",
        action="append",
        choices=INSIGHT_QUALITY_DIMENSIONS,
        dest="reason_dimensions",
    )
    return parser


def run_review(args: argparse.Namespace, *, connection: Any) -> dict[str, Any]:
    summary = live_acceptance.load_acceptance_summary(args.acceptance_summary)
    if summary["terminal_state"]["acceptance_status"] != "passed":
        raise ValueError("insight_quality_acceptance_summary_not_passed")
    case = summary["case"]
    execution = summary["execution"]
    source_publication = summary["publication"]
    evaluation_case = InsightEvaluationCaseSnapshot.create(
        acceptance_summary_version=summary["schema_version"],
        acceptance_source=summary["acceptance_source"],
        acceptance_summary_digest=canonical_digest(summary),
        acceptance_status=summary["terminal_state"]["acceptance_status"],
        case_id=case["case_id"],
        question_family=case["question_family"],
        variant=case["variant"],
        user_message=case["user_message"],
        review_focus=case["review_focus"],
        run_attempt_id=execution["final_run_id"],
        publication_ref=source_publication["publication_ref"],
        publication_digest=source_publication["publication_digest"],
        customer_payload_ref=source_publication["customer_payload_ref"],
        customer_payload_digest=source_publication["customer_payload_digest"],
    )
    loaded = load_insight_review_publication(
        connection,
        publication_ref=evaluation_case.publication_ref,
    )
    publication = loaded.publication
    if (
        publication.run_attempt_id != evaluation_case.run_attempt_id
        or publication.publication_digest != evaluation_case.publication_digest
    ):
        raise ValueError("insight_quality_acceptance_publication_conflict")
    request = None
    if args.requested_attempt_id is not None:
        request = NarrativeAttemptRequest.create(
            publication=publication,
            requested_attempt_id=args.requested_attempt_id,
            reason_dimensions=args.reason_dimensions,
            requested_by=args.reviewer_ref,
        )
    scores = {
        "explanation_value": args.explanation_value,
        "novelty": args.novelty,
        "decision_usefulness": args.decision_usefulness,
        "competing_hypotheses": args.competing_hypotheses,
        "uncertainty_handling": args.uncertainty_handling,
        "actionability": args.actionability,
    }
    human_reasons = {
        "explanation_value": args.explanation_value_reason,
        "novelty": args.novelty_reason,
        "decision_usefulness": args.decision_usefulness_reason,
        "competing_hypotheses": args.competing_hypotheses_reason,
        "uncertainty_handling": args.uncertainty_handling_reason,
        "actionability": args.actionability_reason,
    }
    reviewed_at = args.reviewed_at or datetime.now(timezone.utc)
    evaluation = InsightQualityEvaluation.review(
        publication=publication,
        rubric=InsightQualityRubric.v1(),
        evaluation_case=evaluation_case,
        model_profile=loaded.model_profile,
        reviewer_ref=args.reviewer_ref,
        scores=scores,
        human_reasons=human_reasons,
        narrative_attempt_request=request,
        reviewed_at=reviewed_at,
    )
    persisted = persist_insight_quality_evaluation(
        connection,
        owner_ref=loaded.owner_ref,
        publication=publication,
        evaluation=evaluation,
        narrative_attempt_request=request,
    )
    return {
        "ok": True,
        "status": persisted.status,
        "run_attempt_id": persisted.run_attempt_id,
        "publication_ref": publication.publication_ref,
        "evaluation_ref": persisted.evaluation_ref,
        "reviewer_ref": evaluation.reviewer_ref,
        "rubric_ref": evaluation.rubric_ref,
        "rubric_digest": evaluation.rubric_digest,
        "evaluation_case_ref": evaluation.evaluation_case_ref,
        "evaluation_case_digest": evaluation.evaluation_case_digest,
        "model_profile_ref": evaluation.model_profile_ref,
        "model_profile_digest": evaluation.model_profile_digest,
        "scores": dict(evaluation.scores),
        "human_reasons": dict(evaluation.human_reasons),
        "result": evaluation.result,
        "reviewed_at": evaluation.reviewed_at,
        "narrative_attempt_request_ref": (persisted.narrative_attempt_request_ref),
    }


def main(
    argv: list[str] | None = None,
    *,
    connection_factory: Callable[[], Any] = _connect_runtime_database,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.requested_attempt_id is None) != (args.reason_dimensions is None):
        parser.error(
            "--requested-attempt-id and at least one --reason-dimension "
            "must be provided together"
        )
    connection = connection_factory()
    try:
        result = run_review(args, connection=connection)
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
