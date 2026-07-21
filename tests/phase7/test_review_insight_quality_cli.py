from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from bi_agent.runtime.insight_governance_persistence import (
    InsightGovernancePersistenceError,
    load_insight_review_publication,
)
from tests.phase7.test_insight_governance_persistence import _Connection
from tests.phase7.test_live_customer_publication_acceptance import (
    _build as _build_acceptance_summary,
)
from tests.phase7.test_publication_authority import QUALITY_REASONS, _context
from tools.phase7 import review_insight_quality
from tools.phase7 import run_live_conversation_system_test as live_acceptance


class _ReviewConnection(_Connection):
    def __init__(self, publications: tuple[tuple[str, Any], ...]) -> None:
        super().__init__(publications)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _write_acceptance_summary(
    tmp_path: Path,
    publication: Any,
    *,
    acceptance_status: str = "passed",
) -> Path:
    summary = deepcopy(_build_acceptance_summary())
    summary["execution"]["run_ids"] = [publication.run_attempt_id]
    summary["execution"]["final_run_id"] = publication.run_attempt_id
    summary["publication"]["publication_ref"] = publication.publication_ref
    summary["publication"]["publication_digest"] = publication.publication_digest
    if acceptance_status != "passed":
        summary["terminal_state"]["acceptance_status"] = acceptance_status
        summary["terminal_state"]["reason"] = "review_source_not_accepted"
    live_acceptance.validate_acceptance_summary(summary)
    path = tmp_path / "acceptance-summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    return path


def _arguments(acceptance_summary: Path) -> list[str]:
    return [
        "--acceptance-summary",
        str(acceptance_summary),
        "--reviewer-ref",
        "reviewer:business-42",
        "--explanation-value",
        "5",
        "--explanation-value-reason",
        QUALITY_REASONS["explanation_value"],
        "--novelty",
        "4",
        "--novelty-reason",
        QUALITY_REASONS["novelty"],
        "--decision-usefulness",
        "5",
        "--decision-usefulness-reason",
        QUALITY_REASONS["decision_usefulness"],
        "--competing-hypotheses",
        "3",
        "--competing-hypotheses-reason",
        QUALITY_REASONS["competing_hypotheses"],
        "--uncertainty-handling",
        "4",
        "--uncertainty-handling-reason",
        QUALITY_REASONS["uncertainty_handling"],
        "--actionability",
        "5",
        "--actionability-reason",
        QUALITY_REASONS["actionability"],
        "--reviewed-at",
        "2026-07-18T16:00:00Z",
    ]


def test_cli_derives_typed_review_context_and_persists_explicit_human_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    publication = _context().publication
    summary_path = _write_acceptance_summary(tmp_path, publication)
    connection = _ReviewConnection((("owner:phase6-review", publication),))

    exit_code = review_insight_quality.main(
        _arguments(summary_path),
        connection_factory=lambda: connection,
    )

    assert exit_code == 0
    assert connection.closed is True
    assert len(connection.tables["insight_quality_evaluations"]) == 1
    assert "narrative_attempt_requests" not in connection.tables
    assert "guardrail_promotion_records" not in connection.tables
    persisted = connection.tables["insight_quality_evaluations"][0]
    assert persisted["scores"] == {
        "explanation_value": 5,
        "novelty": 4,
        "decision_usefulness": 5,
        "competing_hypotheses": 3,
        "uncertainty_handling": 4,
        "actionability": 5,
    }
    assert persisted["human_reasons"] == QUALITY_REASONS
    assert persisted["reviewer_ref"] == "reviewer:business-42"
    assert (
        persisted["evaluation_case"]["case_id"]
        == (_build_acceptance_summary()["case"]["case_id"])
    )
    writer_authority = connection.tables["narrative_writer_attempts"][0]
    assert (
        persisted["model_profile"]["writer_attempt_ref"]
        == (writer_authority["writer_attempt_ref"])
    )
    assert (
        persisted["model_profile"]["model_ref"]
        == (writer_authority["payload"]["model_ref"])
    )
    assert persisted["rubric"]["rubric_version"] == "insight-quality-rubric.v1"
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "inserted"
    assert output["result"] == "retain_publication"
    assert output["publication_ref"] == publication.publication_ref
    assert output["narrative_attempt_request_ref"] is None
    assert output["human_reasons"] == QUALITY_REASONS


def test_cli_persists_only_an_explicit_independent_attempt_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    publication = _context().publication
    summary_path = _write_acceptance_summary(tmp_path, publication)
    connection = _ReviewConnection((("owner:phase6-review", publication),))
    arguments = [
        *_arguments(summary_path),
        "--requested-attempt-id",
        "writer-attempt:independent-human-review",
        "--reason-dimension",
        "novelty",
        "--reason-dimension",
        "competing_hypotheses",
    ]

    exit_code = review_insight_quality.main(
        arguments,
        connection_factory=lambda: connection,
    )

    assert exit_code == 0
    assert len(connection.tables["narrative_attempt_requests"]) == 1
    request = connection.tables["narrative_attempt_requests"][0]
    assert request["requested_attempt_id"] == (
        "writer-attempt:independent-human-review"
    )
    assert request["reason_dimensions"] == [
        "competing_hypotheses",
        "novelty",
    ]
    assert request["requested_by"] == "reviewer:business-42"
    assert "guardrail_promotion_records" not in connection.tables
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "request_independent_narrative_attempt"
    assert output["narrative_attempt_request_ref"] == request["request_ref"]


@pytest.mark.parametrize(
    "extra_arguments",
    [
        ["--requested-attempt-id", "writer-attempt:independent-human-review"],
        ["--reason-dimension", "novelty"],
    ],
)
def test_cli_rejects_partial_independent_attempt_input(
    tmp_path: Path,
    extra_arguments: list[str],
) -> None:
    publication = _context().publication
    summary_path = _write_acceptance_summary(tmp_path, publication)

    with pytest.raises(SystemExit, match="2"):
        review_insight_quality.main(
            [*_arguments(summary_path), *extra_arguments],
            connection_factory=lambda: pytest.fail("database must not be opened"),
        )


def test_cli_rejects_acceptance_summary_that_did_not_pass(tmp_path: Path) -> None:
    publication = _context().publication
    summary_path = _write_acceptance_summary(
        tmp_path,
        publication,
        acceptance_status="contract_failed",
    )
    connection = _ReviewConnection((("owner:phase6-review", publication),))

    with pytest.raises(
        ValueError,
        match="insight_quality_acceptance_summary_not_passed",
    ):
        review_insight_quality.main(
            _arguments(summary_path),
            connection_factory=lambda: connection,
        )

    assert connection.closed is True
    assert "insight_quality_evaluations" not in connection.tables


def test_cli_has_no_operator_supplied_case_or_model_reference_flags() -> None:
    destinations = {
        action.dest for action in review_insight_quality.build_parser()._actions
    }

    assert "publication_ref" not in destinations
    assert "evaluation_case_ref" not in destinations
    assert "model_profile_ref" not in destinations
    assert "acceptance_summary" in destinations


def test_cli_rejects_blank_human_reason_before_opening_database(
    tmp_path: Path,
) -> None:
    publication = _context().publication
    summary_path = _write_acceptance_summary(tmp_path, publication)
    arguments = _arguments(summary_path)
    reason_index = arguments.index("--novelty-reason") + 1
    arguments[reason_index] = ""

    with pytest.raises(SystemExit, match="2"):
        review_insight_quality.main(
            arguments,
            connection_factory=lambda: pytest.fail("database must not be opened"),
        )


def test_loader_rejects_publication_payload_drift() -> None:
    publication = _context().publication
    connection = _ReviewConnection((("owner:phase6-review", publication),))
    connection.tables["publication_revisions"][0]["payload"]["projection_id"] = (
        "publication-projection:drifted"
    )

    with pytest.raises(
        InsightGovernancePersistenceError,
        match="insight_quality_source_publication_conflict",
    ):
        load_insight_review_publication(
            connection,
            publication_ref=publication.publication_ref,
        )
