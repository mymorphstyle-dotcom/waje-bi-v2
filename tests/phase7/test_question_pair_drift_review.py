from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from bi_agent.runtime.llm_client import LLMResult
from tools.phase7 import review_question_pair_drift as pair_review
from tools.phase7 import run_live_conversation_system_test as live


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "evals" / "phase7" / "business_question_expectations.yaml"
FAMILY = "pattern_explanation"


def _digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _provider_hash(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _manifest() -> dict[str, Any]:
    return live.load_manifest(str(MANIFEST_PATH))


def _publication(text: str) -> dict[str, Any]:
    return {
        "blocks": [
            {
                "role": "executive_answer",
                "text": text,
                "statement_role": "business_finding",
                "claim_refs": ["claim:verified"],
                "recommendation_refs": [],
                "limitation_refs": ["limitation:association-only"],
                "material_fact_bindings": [
                    {
                        "name": "付费金额变化",
                        "fact_kind": "delta",
                        "value": "12.0",
                        "range_end": None,
                        "unit": "%",
                    }
                ],
            }
        ],
        "claim_refs": ["claim:verified"],
        "limitation_refs": ["limitation:association-only"],
        "recommendation_refs": [],
        "visualization_refs": [],
        "warnings": [],
        "field_visibility_policy_ref": "visibility:fixed",
    }


def _pair_material_snapshot(
    variant: str,
    *,
    scope: Mapping[str, Any] | None = None,
    time_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    decision_ref = "decision:" + variant
    return live.build_pair_material_snapshot(
        intent_revision_id="intent:" + variant,
        plan_revision_id="plan:" + variant,
        target_metric_refs=["paid_amount"],
        analysis_axes=[
            {
                "axis_id": "pattern_validation",
                "target_metric_refs": ["paid_amount"],
                "metric_refs": ["paid_amount", "paid_user_count"],
            }
        ],
        scope=dict(scope or {"type": "full_sample"}),
        intent_time_spec=dict(
            time_spec
            or {
                "kind": "date_range",
                "start": "2024-01-01",
                "end": "2026-06-30",
            }
        ),
        resolved_window_refs=["window:month-start", "window:month-mid-end"],
        context_window_specs=[],
        plan_decision_refs=[decision_ref],
        active_decisions=[
            {
                "decision_ref": decision_ref,
                "slot_id": "comparison_windows",
                "option_id": "month_start_vs_mid_end",
                "source": "system",
                "status": "inferred",
                "materiality": "material",
                "value": {"comparison": "days_1_10_vs_days_11_plus"},
                "affected_plan_fields": ["resolved_window_refs"],
            }
        ],
        user_required_obligations=[
            {
                "obligation_id": "obligation:pattern",
                "role": "user_required",
                "claim_kind": "temporal_pattern",
                "subject": {
                    "target_metric_ref": "paid_amount",
                    "scope": {"type": "full_sample"},
                    "outcome_refs": ["pattern_and_exceptions"],
                },
                "success_policy": {"minimum_claim_strength": "directional"},
            }
        ],
        obligation_closure=[
            {
                "obligation_id": "obligation:pattern",
                "coverage_state": "satisfied",
                "coverage_claim_refs": ["claim:verified"],
                "coverage_limitation_refs": ["limitation:association-only"],
                "unavailable_limitation_refs": [],
            }
        ],
    )


def _summary(
    variant: str,
    *,
    publication: Mapping[str, Any] | None = None,
    release_refs: list[str] | None = None,
    snapshot_refs: list[str] | None = None,
    material_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pair_manifest = pair_review.build_pair_manifest(_manifest(), FAMILY)
    customer_publication = dict(
        publication or _publication("原句答案" if variant == "original" else "改写答案")
    )
    pair_snapshot = dict(material_snapshot or _pair_material_snapshot(variant))
    return {
        "schema_version": live.ACCEPTANCE_SUMMARY_VERSION,
        "acceptance_source": live.ACCEPTANCE_SOURCE,
        "case": dict(pair_manifest[variant]),
        "dependency_health": {
            "checked_at": "2026-07-20T00:00:00Z",
            "overall_status": "ok",
            "checks": [],
        },
        "execution": {
            "thread_id": "thread:" + variant,
            "run_ids": ["run:" + variant],
            "final_run_id": "run:" + variant,
        },
        "active_release_refs": {
            "actual_as_of": "2026-07-20T00:00:00Z",
            "release_refs": release_refs or ["release:paid-success"],
            "snapshot_refs": snapshot_refs or ["snapshot:paid-success"],
        },
        "authority_refs": {
            "intent_revision_id": pair_snapshot["intent_revision_id"],
            "authority_context_ref": "context:" + variant,
            "authority_context_digest": "a" * 64,
            "plan_revision_id": pair_snapshot["plan_revision_id"],
            "execution_result_ref": "execution:" + variant,
            "authority_bundle_ref": "bundle:" + variant,
            "authority_bundle_digest": "b" * 64,
        },
        "pair_material_snapshot": pair_snapshot,
        "publication": {
            "state": "published",
            "customer_payload_ref": "payload:" + variant,
            "customer_payload_digest": _digest(customer_publication),
            "publication_ref": "publication:" + variant,
            "publication_digest": "c" * 64,
            "projection_id": "projection:" + variant,
            "projection_digest": "d" * 64,
            "customer_publication_event_observed": True,
        },
        "delivery": {
            "state": "published",
            "outbox_ref": "outbox:" + variant,
            "attempt_ref": "attempt:" + variant,
            "customer_publication_ref": "customer-publication:" + variant,
            "failure_code": None,
        },
        "llm_call_audits": [],
        "human_decisions": [],
        "terminal_state": {
            "run_status": "completed",
            "publication_state": "published",
            "delivery_state": "published",
            "acceptance_status": "passed",
            "reason": "persisted_customer_publication_verified",
        },
    }


def _equivalent_decision() -> dict[str, Any]:
    return {
        "pair_status": "equivalent",
        "direction_consistent": True,
        "magnitude_consistent": True,
        "baseline_consistent": True,
        "evidence_strength_consistent": True,
        "boundary_consistent": True,
        "material_coverage_consistent": True,
        "metric_consistent": True,
        "scope_consistent": True,
        "time_semantics_consistent": True,
        "material_decision_consistent": True,
        "required_obligation_consistent": True,
        "drift_reasons": [],
        "summary": "两种表达发布了同一业务结论和证据边界。",
    }


class _LLM:
    def __init__(self, output: Mapping[str, Any]) -> None:
        self.output = dict(output)
        self.calls = 0

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        self.calls += 1
        kwargs["output_validator"](self.output)
        return LLMResult(
            output=self.output,
            audit={
                "task": kwargs["task"],
                "provider": "openai_compatible",
                "model": "deepseek-chat",
                "model_tier": kwargs["model_tier"],
                "thinking": kwargs["thinking"],
                "prompt_version": kwargs["prompt_version"],
                "required_keys": list(kwargs["required_keys"]),
                "response_id": "response:pair-review",
                "attempt_count": 1,
                "input_hash": _provider_hash(kwargs["messages"]),
                "output_hash": _provider_hash(self.output),
                "started_at": "2026-07-20T00:00:00Z",
                "finished_at": "2026-07-20T00:00:01Z",
                "duration_ms": 1000,
                "usage": {},
            },
        )


def _review(
    *,
    original_publication: Mapping[str, Any] | None = None,
    paraphrase_publication: Mapping[str, Any] | None = None,
    original_summary: Mapping[str, Any] | None = None,
    paraphrase_summary: Mapping[str, Any] | None = None,
    llm: _LLM | None = None,
) -> tuple[dict[str, Any], _LLM]:
    original = dict(original_publication or _publication("原句答案"))
    paraphrase = dict(paraphrase_publication or _publication("改写答案"))
    client = llm or _LLM(_equivalent_decision())
    result = pair_review.review_pair(
        original_summary=original_summary or _summary("original", publication=original),
        paraphrase_summary=paraphrase_summary
        or _summary("paraphrase", publication=paraphrase),
        original_publication=original,
        paraphrase_publication=paraphrase,
        expectation_manifest=_manifest(),
        llm_client=client,
    )
    return result, client


def test_pair_review_allows_different_wording_and_structure(tmp_path: Path) -> None:
    result, llm = _review(
        original_publication=_publication("方向上升，边界明确。"),
        paraphrase_publication=_publication("证据支持抬升，但不能作因果解释。"),
    )

    assert result["decision"]["pair_status"] == "equivalent"
    assert result["authority_comparable"] is True
    assert result["provider_audit"]["model"] == "deepseek-chat"
    assert result["pair_manifest"]["original"]["variant"] == "original"
    assert result["review_input"]["original"]["pair_material_snapshot"][
        "metric_refs"
    ] == ["paid_amount", "paid_user_count"]
    assert llm.calls == 1

    path = tmp_path / "pair.json"
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    assert (
        pair_review.load_pair_review_artifact(
            path,
            expectation_manifest=_manifest(),
        )
        == result
    )


def test_pair_review_fails_closed_when_authority_context_differs() -> None:
    original_publication = _publication("原句答案")
    paraphrase_publication = _publication("改写答案")
    paraphrase = _summary(
        "paraphrase",
        publication=paraphrase_publication,
        snapshot_refs=["snapshot:new"],
    )
    llm = _LLM(_equivalent_decision())

    result, _ = _review(
        original_publication=original_publication,
        paraphrase_publication=paraphrase_publication,
        original_summary=_summary("original", publication=original_publication),
        paraphrase_summary=paraphrase,
        llm=llm,
    )

    assert result["decision"]["pair_status"] == "material_drift"
    assert all(
        result["decision"][field] is False for field in pair_review.CONSISTENCY_FIELDS
    )
    assert result["authority_comparable"] is False
    assert result["provider_audit"] is None
    assert llm.calls == 0


def test_pair_decision_rejects_equivalence_with_material_contract_drift() -> None:
    invalid = _equivalent_decision()
    invalid["material_decision_consistent"] = False

    with pytest.raises(
        ValueError,
        match="^question_pair_decision_equivalence_invalid$",
    ):
        _review(llm=_LLM(invalid))


def test_pair_review_requires_exact_declared_manifest_cases() -> None:
    publication = _publication("改写答案")
    paraphrase = _summary("paraphrase", publication=publication)
    paraphrase["case"]["user_message"] += "额外语义"

    with pytest.raises(
        ValueError,
        match="^question_pair_source_manifest_mismatch$",
    ):
        _review(
            paraphrase_publication=publication,
            paraphrase_summary=paraphrase,
        )


def test_pair_review_does_not_mutate_source_summaries() -> None:
    original_publication = _publication("原句答案")
    paraphrase_publication = _publication("改写答案")
    original = _summary("original", publication=original_publication)
    paraphrase = _summary("paraphrase", publication=paraphrase_publication)
    expected_original = deepcopy(original)
    expected_paraphrase = deepcopy(paraphrase)

    _review(
        original_publication=original_publication,
        paraphrase_publication=paraphrase_publication,
        original_summary=original,
        paraphrase_summary=paraphrase,
    )

    assert original == expected_original
    assert paraphrase == expected_paraphrase


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("metric_refs",), ["gross_revenue"]),
        (("scope", "type"), "filtered_sample"),
        (("time_semantics", "intent_time_spec", "start"), "2025-01-01"),
        (
            ("active_material_decisions", 0, "option_id"),
            "month_end_only",
        ),
        (
            (
                "user_required_obligation_coverage",
                0,
                "minimum_claim_strength",
            ),
            "causal",
        ),
    ],
)
def test_pair_artifact_rejects_nested_material_snapshot_tampering(
    path: tuple[Any, ...],
    replacement: Any,
) -> None:
    artifact, _ = _review()
    tampered = deepcopy(artifact)
    target: Any = tampered["review_input"]["original"]["pair_material_snapshot"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement

    with pytest.raises(ValueError):
        pair_review.validate_pair_review_artifact(
            tampered,
            expectation_manifest=_manifest(),
        )


def test_pair_artifact_rejects_manifest_input_and_content_digest_tampering() -> None:
    artifact, _ = _review()

    manifest_tamper = deepcopy(artifact)
    manifest_tamper["pair_manifest"]["original"]["review_focus"] += " changed"
    with pytest.raises(ValueError):
        pair_review.validate_pair_review_artifact(
            manifest_tamper,
            expectation_manifest=_manifest(),
        )

    input_tamper = deepcopy(artifact)
    input_tamper["input_digest"] = "0" * 64
    with pytest.raises(
        ValueError,
        match="^question_pair_artifact_input_digest_invalid$",
    ):
        pair_review.validate_pair_review_artifact(
            input_tamper,
            expectation_manifest=_manifest(),
        )

    content_tamper = deepcopy(artifact)
    content_tamper["content_digest"] = "0" * 64
    with pytest.raises(
        ValueError,
        match="^question_pair_artifact_digest_invalid$",
    ):
        pair_review.validate_pair_review_artifact(
            content_tamper,
            expectation_manifest=_manifest(),
        )


@pytest.mark.parametrize("field", ["input_hash", "output_hash"])
def test_pair_artifact_rejects_provider_audit_hash_tampering(field: str) -> None:
    artifact, _ = _review()
    artifact["provider_audit"][field] = "0" * 64

    with pytest.raises(
        ValueError, match=f"question_pair_provider_audit_{field.split('_')[0]}_invalid"
    ):
        pair_review.validate_pair_review_artifact(
            artifact,
            expectation_manifest=_manifest(),
        )


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_cli_writes_and_strictly_reads_back_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_publication = _publication("原句答案")
    paraphrase_publication = _publication("改写答案")
    original = _summary("original", publication=original_publication)
    paraphrase = _summary("paraphrase", publication=paraphrase_publication)
    expectation_manifest = _manifest()
    summaries = iter((original, paraphrase))
    publications = iter((original_publication, paraphrase_publication))
    connection = _Connection()
    monkeypatch.setattr(pair_review.live, "load_env_file", lambda _: [])
    monkeypatch.setattr(
        pair_review.live,
        "load_manifest",
        lambda _: expectation_manifest,
    )
    monkeypatch.setattr(pair_review, "_load_summary", lambda _: next(summaries))
    monkeypatch.setattr(
        pair_review,
        "_validated_persisted_source",
        lambda _connection, _summary: next(publications),
    )

    exit_code = pair_review.main(
        [
            "--original-summary",
            "original.json",
            "--paraphrase-summary",
            "paraphrase.json",
            "--artifact-dir",
            str(tmp_path),
        ],
        connection_factory=lambda: connection,
        llm_factory=lambda: _LLM(_equivalent_decision()),
    )

    assert exit_code == 0
    assert connection.closed is True
    artifacts = list(tmp_path.glob("pair-*.json"))
    assert len(artifacts) == 1
    loaded = pair_review.load_pair_review_artifact(
        artifacts[0],
        expectation_manifest=_manifest(),
    )
    assert loaded["decision"]["pair_status"] == "equivalent"
