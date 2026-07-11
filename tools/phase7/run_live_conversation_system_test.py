from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.runtime.claim_provenance import (
    validate_trusted_claim_provenance_record,
    validate_verified_claim_record,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_value


def load_cases(path: str) -> list[dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return []
    cases = raw.get("conversation_cases", [])
    return [case for case in cases if isinstance(case, dict) and case.get("id")]


def select_cases(cases: list[dict[str, Any]], case_id: str | None) -> list[dict[str, Any]]:
    if not case_id:
        return cases
    return [case for case in cases if case["id"] == case_id]


def load_env_file(path: str = ".env") -> list[str]:
    env_path = Path(path)
    if not env_path.exists():
        return []
    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value.strip())
        loaded.append(key)
    return loaded


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        return value.split(" #", 1)[0].strip()
    return value


def _effective_result(turn_record: dict[str, Any]) -> dict[str, Any]:
    if turn_record.get("resumed_status"):
        return {
            "status": turn_record.get("resumed_status"),
            "run_id": turn_record.get("resumed_run_id"),
            "topic_id": turn_record.get("resumed_topic_id"),
            "intent": turn_record.get("resumed_intent"),
            "topic_relation": turn_record.get("resumed_topic_relation"),
            "failure_reason": turn_record.get("resumed_failure_reason"),
            "answer_package": turn_record.get("resumed_answer_package"),
            "context_manifest": turn_record.get("resumed_context_manifest"),
            "accepted_graph": turn_record.get("resumed_accepted_graph") or [],
            "llm_calls": turn_record.get("resumed_llm_calls", []),
            "quality_review": turn_record.get("resumed_quality_review"),
            "artifact_path": turn_record.get("resumed_artifact_path"),
        }
    return {
        "status": turn_record.get("status"),
        "run_id": turn_record.get("run_id"),
        "topic_id": turn_record.get("topic_id"),
        "intent": turn_record.get("intent"),
        "topic_relation": turn_record.get("topic_relation"),
        "answer_package": turn_record.get("answer_package"),
        "context_manifest": turn_record.get("context_manifest"),
        "accepted_graph": turn_record.get("accepted_graph") or [],
        "llm_calls": turn_record.get("llm_calls", []),
        "quality_review": turn_record.get("quality_review"),
        "artifact_path": turn_record.get("artifact_path"),
    }


def _automatic_clarification_response(result: Mapping[str, Any]) -> str:
    clarification = result.get("clarification") or {}
    actions = tuple(
        item
        for item in clarification.get("choice_actions") or ()
        if isinstance(item, Mapping)
    )
    progress_order = (
        "choose_supported_claim_intent",
        "choose_supported_window",
        "use_permitted_aggregate",
        "use_supported_grain",
        "remove_dimension_path",
        "omit_unavailable_context",
    )
    for action_kind in progress_order:
        label = next(
            (
                str(item.get("business_label") or "").strip()
                for item in actions
                if item.get("action_kind") == action_kind
                and str(item.get("business_label") or "").strip()
            ),
            "",
        )
        if label:
            return label
    raw_recommended = clarification.get("recommended_assumption") or {}
    recommended = str(
        (
            raw_recommended.get("option")
            or raw_recommended.get("assumption")
            or ""
        )
        if isinstance(raw_recommended, Mapping)
        else raw_recommended
    ).strip()
    question_options = tuple(
        str(option).strip()
        for question in clarification.get("questions") or ()
        if isinstance(question, Mapping)
        for option in question.get("options") or ()
        if str(option).strip()
    )
    if recommended and (
        not question_options or recommended in question_options
    ):
        return recommended
    first_progressing_option = next(
        (
            option
            for option in question_options
            if option != "tell the agent to do differently"
        ),
        "",
    )
    if first_progressing_option:
        return first_progressing_option
    return "按推荐继续"


def _review_expectations(turn: dict[str, Any], turn_record: dict[str, Any]) -> dict[str, Any]:
    effective = _effective_result(turn_record)
    return _expectation_review(turn, turn_record, effective, effective.get("accepted_graph") or [])


def _expectation_review(
    turn: dict[str, Any],
    turn_record: dict[str, Any],
    effective_result: dict[str, Any],
    effective_graph: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    expect = turn.get("expect") or {}
    required = list(
        dict.fromkeys(
            list(expect.get("required_capabilities", []))
            + list(expect.get("major_nodes", []))
        )
    )
    actual = list(effective_graph or [])
    missing = [capability for capability in required if capability not in actual]
    actual_intent = str(turn_record.get("intent") or effective_result.get("intent") or "")
    actual_relation = str(
        turn_record.get("topic_relation") or effective_result.get("topic_relation") or ""
    )
    missing_answer_text = [
        text
        for text in expect.get("final_answer_contains", [])
        if text not in _answer_text(effective_result.get("answer_package") or {})
    ]
    missing_hard_boundary_text = [
        text
        for text in expect.get("hard_boundary_final_answer_contains", [])
        if text not in _answer_text(effective_result.get("answer_package") or {})
    ]
    manifest = effective_result.get("context_manifest")
    manifest_present = isinstance(manifest, dict) and bool(manifest)
    claim_review = _claim_evidence_review(
        effective_result.get("answer_package") or {},
        manifest if isinstance(manifest, dict) else {},
        requires_claims=_expectation_requires_claims(expect),
    )
    manifest_can_support_claims = bool(manifest.get("can_support_claims")) if isinstance(manifest, dict) else False
    claim_support_ok = manifest_present and manifest_can_support_claims and claim_review["passed"]
    clarification_ok = True
    if expect.get("allow_clarification"):
        clarification_ok = (
            turn_record.get("status") == "waiting_for_clarification"
            and bool(turn_record.get("clarification_response"))
            and bool(turn_record.get("resumed_status"))
        )
    intent_ok = not expect.get("intent") or actual_intent == expect.get("intent")
    relation_ok = _topic_relation_matches(expect.get("topic_relation"), actual_relation)
    return {
        "expected_intent": expect.get("intent"),
        "actual_intent": actual_intent,
        "intent_passed": intent_ok,
        "expected_topic_relation": expect.get("topic_relation"),
        "actual_topic_relation": actual_relation,
        "topic_relation_passed": relation_ok,
        "allow_clarification": bool(expect.get("allow_clarification")),
        "clarification_passed": clarification_ok,
        "final_answer_contains": list(expect.get("final_answer_contains", [])),
        "missing_final_answer_text": missing_answer_text,
        "hard_boundary_final_answer_contains": list(
            expect.get("hard_boundary_final_answer_contains", [])
        ),
        "missing_hard_boundary_final_answer_text": missing_hard_boundary_text,
        "context_manifest_present": manifest_present,
        "context_manifest_can_support_claims": manifest_can_support_claims,
        "claim_support_policy_passed": claim_support_ok,
        "claim_evidence_review": claim_review,
        "required_capabilities": required,
        "missing_required_capabilities": missing,
        "expected_result_reuse": expect.get("result_reuse"),
        "expected_context_use": list(expect.get("context_use", [])),
        "expected_answer_boundary": expect.get("answer_boundary"),
        "major_nodes": list(expect.get("major_nodes", [])),
        "passed": (
            intent_ok
            and relation_ok
            and clarification_ok
            and manifest_present
            and claim_support_ok
            and not missing
            and not missing_hard_boundary_text
        ),
    }


def _expectation_requires_claims(expect: dict[str, Any]) -> bool:
    return bool(
        expect.get("final_answer_contains")
        or expect.get("hard_boundary_final_answer_contains")
        or expect.get("answer_boundary")
    )


def _topic_relation_matches(expected: str | None, actual: str) -> bool:
    if not expected:
        return True
    aliases = {
        "create": {"new_topic"},
        "inherit": {"inherit_current"},
    }
    return actual == expected or actual in aliases.get(expected, set())


def _answer_text(answer_package: dict[str, Any]) -> str:
    parts: list[str] = []
    final_answer = answer_package.get("final_answer")
    if isinstance(final_answer, str):
        parts.append(final_answer)
    for section in answer_package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        for key in ("answer_text", "final_business_summary"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _quality_review(answer_package: dict[str, Any]) -> dict[str, Any]:
    quality_gate = answer_package.get("quality_gate") if isinstance(answer_package, dict) else {}
    if not isinstance(quality_gate, dict):
        quality_gate = {}
    issues = list(quality_gate.get("issues") or ())
    final_summary_warnings = list(quality_gate.get("final_summary_display_warnings") or ())
    soft_warnings = list(
        dict.fromkeys(
            [
                str(item)
                for item in (
                    *issues,
                    *list(quality_gate.get("repairable_warnings") or ()),
                    *final_summary_warnings,
                )
                if item
            ]
        )
    )
    return {
        "blocks_display": bool(quality_gate.get("blocks_display")),
        "display_status": str(quality_gate.get("display_status") or ""),
        "final_answer_audit_warnings": list(quality_gate.get("repairable_warnings") or ()),
        "quality_gate_issues": issues,
        "final_summary_display_warnings": final_summary_warnings,
        "quality_warnings": soft_warnings,
        "risk_markers": list(quality_gate.get("risk_flags") or ()),
        "direct_answer": bool(quality_gate.get("direct_answer")),
        "has_verified_claims": bool(quality_gate.get("has_verified_claims")),
        "verified_claim_preserved": bool(quality_gate.get("verified_claim_preserved")),
        "business_insight_present": bool(quality_gate.get("business_insight_present")),
        "followups_one_intent": bool(quality_gate.get("followups_one_intent")),
    }


def _strict_quality_failed(turn_record: dict[str, Any]) -> bool:
    effective = _effective_result(turn_record)
    expectation = turn_record.get("expectation_review") or {}
    review = effective.get("quality_review")
    if not isinstance(review, dict) or not review:
        review = _quality_review(effective.get("answer_package") or {})
    if not isinstance(review, dict) or not review:
        return True
    if expectation.get("missing_required_capabilities"):
        return True
    if expectation.get("missing_hard_boundary_final_answer_text"):
        return True
    if expectation.get("claim_support_policy_passed") is False:
        return True
    return bool(review.get("blocks_display"))


def _real_clickhouse_review(
    result: dict[str, Any],
    *,
    real_clickhouse: bool,
    evidence_resolver: Any = None,
    required_datasets: tuple[str, ...] | list[str] = (),
    analysis_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    package = _runtime_audit_package(result)
    if not real_clickhouse:
        return {
            "required": False,
            "real_clickhouse_verified": True,
            "clickhouse_result_refs": [],
            "observed_datasets": [],
            "runtime_correctness": {
                "all_required_queries_complete": True,
                "all_capabilities_bound": True,
                "all_claims_traceable": True,
            },
            "issues": [],
        }

    issues: list[str] = []
    result_refs: set[str] = set()
    observed_datasets: set[str] = set()
    evidence_items = {
        str(item.get("evidence_ref") or ""): item
        for section in package.get("sections") or ()
        if isinstance(section, Mapping)
        for item in (section.get("payload") or {}).get("evidence") or ()
        if isinstance(item, Mapping) and item.get("evidence_ref")
    }
    binding_refs = {
        str(item.get("binding_manifest_ref") or "")
        for item in evidence_items.values()
        if item.get("binding_manifest_ref")
    }
    if evidence_resolver is None:
        issues.append("missing_runtime_authority_resolver")
    if not binding_refs:
        issues.append("missing_authoritative_capability_bindings")

    resolved_evidence_refs: set[str] = set()
    for binding_ref in sorted(binding_refs):
        try:
            binding = evidence_resolver.resolve_capability_binding(binding_ref)
        except Exception as exc:
            issues.append(
                f"capability_binding_authority_error:{binding_ref}:{type(exc).__name__}"
            )
            continue
        if binding is None:
            issues.append(f"missing_capability_binding:{binding_ref}")
            continue
        if binding.status not in {"ready", "degraded"}:
            issues.append(f"unready_capability_binding:{binding.capability_id}")
        required_query_policies, readiness_policy_issues = (
            _required_query_readiness_policies(binding.plan_payload)
        )
        issues.extend(
            f"capability_binding_readiness_policy_invalid:{binding.capability_id}:{item}"
            for item in readiness_policy_issues
        )
        query_refs = (*binding.query_contract_refs, *binding.validation_query_contract_refs)
        bound_results = (*binding.result_refs, *binding.validation_result_refs)
        completeness_refs = (
            *binding.completeness_record_refs,
            *binding.validation_completeness_record_refs,
        )
        if not (
            len(query_refs) == len(bound_results) == len(completeness_refs)
        ):
            issues.append(f"incomplete_capability_binding:{binding.capability_id}")
            continue
        binding_window_ids: set[str] = set()
        for query_ref, result_ref, completeness_ref in zip(
            query_refs,
            bound_results,
            completeness_refs,
        ):
            result_refs.add(str(result_ref))
            if not str(result_ref).startswith("result:"):
                issues.append(f"legacy_clickhouse_result_ref:{result_ref}")
            try:
                query_record = evidence_resolver.resolve_query_execution(result_ref)
                completeness = evidence_resolver.resolve_completeness(
                    completeness_ref
                )
            except Exception as exc:
                issues.append(
                    f"query_authority_error:{query_ref}:{type(exc).__name__}"
                )
                continue
            if (
                query_record is None
                or query_record.query_contract_ref != query_ref
                or query_record.result_ref != result_ref
            ):
                issues.append(f"missing_clickhouse_query_result:{query_ref}")
                continue
            if query_record.execution_status != "succeeded":
                issues.append(f"failed_clickhouse_query:{query_ref}")
            fixed_bounds = {
                "target_day": (
                    (analysis_context or {}).get("target_date"),
                    (analysis_context or {}).get("target_date"),
                ),
                "previous_day": (
                    (analysis_context or {}).get("previous_day"),
                    (analysis_context or {}).get("previous_day"),
                ),
                "rolling_7_day_baseline": (
                    (analysis_context or {}).get("rolling_7_day_start"),
                    (analysis_context or {}).get("rolling_7_day_end"),
                ),
                "same_weekday_last_week": (
                    (analysis_context or {}).get("same_weekday_last_week"),
                    (analysis_context or {}).get("same_weekday_last_week"),
                ),
                "pattern_history": (
                    (analysis_context or {}).get("pattern_history_start"),
                    (analysis_context or {}).get("target_date"),
                ),
                "anomaly_history": (
                    (analysis_context or {}).get("anomaly_history_start"),
                    (analysis_context or {}).get("previous_day"),
                ),
            }
            for window in query_record.contract.resolved_windows:
                binding_window_ids.add(window.window_id)
                expected = fixed_bounds.get(window.window_id)
                if not expected or not all(expected):
                    continue
                expected_end = (
                    date.fromisoformat(str(expected[1])) + timedelta(days=1)
                ).isoformat()
                if (
                    window.start_inclusive != expected[0]
                    or window.end_exclusive != expected_end
                ):
                    issues.append(f"fixed_window_mismatch:{query_ref}:{window.window_id}")
            if (
                completeness is None
                or completeness.query_contract_ref != query_ref
                or completeness.result_ref != result_ref
            ):
                issues.append(f"missing_clickhouse_completeness:{query_ref}")
                continue
            report = completeness.report_payload
            status = str(report.get("completeness_status") or "")
            readiness = str(report.get("analysis_readiness") or "")
            accepted_completeness = required_query_policies.get(str(query_ref))
            if accepted_completeness is not None and not _report_is_contract_accepted(
                report,
                accepted_completeness=accepted_completeness,
                validation_query=str(query_ref)
                in set(binding.validation_query_contract_refs),
            ):
                issues.append(f"incomplete_clickhouse_query:{query_ref}")
            for snapshot_ref in query_record.source_snapshot_refs:
                try:
                    snapshot_record = evidence_resolver.resolve_snapshot(snapshot_ref)
                except Exception as exc:
                    issues.append(
                        f"snapshot_authority_error:{snapshot_ref}:{type(exc).__name__}"
                    )
                    continue
                if snapshot_record is None:
                    issues.append(f"missing_query_snapshot:{query_ref}:{snapshot_ref}")
                    continue
                snapshot = snapshot_record.snapshot
                observed_datasets.add(snapshot.dataset_id)
                if query_record.contract.permission_scope not in snapshot.permission_scopes:
                    issues.append(f"snapshot_permission_mismatch:{query_ref}")
                for window in query_record.contract.resolved_windows:
                    required_watermark = (
                        date.fromisoformat(window.end_exclusive) - timedelta(days=1)
                    ).isoformat()
                    if snapshot.watermark < required_watermark:
                        issues.append(f"snapshot_window_mismatch:{query_ref}")
        required_history_windows = {
            "pattern_scan": ("pattern_history",),
            "outlier_scan": ("anomaly_history",),
            "outlier_contribution": ("anomaly_history",),
            "high_value_user_contribution": ("anomaly_history",),
        }.get(binding.capability_id, ())
        for window_id in required_history_windows:
            if window_id not in binding_window_ids:
                issues.append(
                    f"fixed_window_missing:{binding.capability_id}:{window_id}"
                )
        for evidence_ref, item in evidence_items.items():
            if str(item.get("binding_manifest_ref") or "") == binding_ref:
                resolved_evidence_refs.add(evidence_ref)

    context_manifest = result.get("context_manifest") or {}
    context_refs = _traceable_refs({}, context_manifest)
    verified_claims = tuple(package.get("verified_claims") or ())
    claims_traceable = not (_claims(package) and not verified_claims)
    if not claims_traceable:
        issues.append("missing_verified_claim_authority")
    for claim_index, claim in enumerate(verified_claims):
        if not isinstance(claim, Mapping):
            claims_traceable = False
            issues.append(f"malformed_verified_claim:{claim_index}")
            continue
        evidence_refs = {
            str(ref) for ref in claim.get("evidence_refs") or () if ref
        }
        claim_results = {str(ref) for ref in claim.get("result_refs") or () if ref}
        provenance_complete = bool(
            claim.get("claim_digest")
            and claim.get("provenance_record_ref")
            and claim.get("context_manifest_ref")
            and claim.get("artifact_refs")
            and claim.get("memory_refs")
            and claim.get("reuse_decisions")
        )
        persisted_claim = None
        trusted_provenance = None
        try:
            resolve_claim = getattr(evidence_resolver, "resolve_verified_claim")
            resolve_provenance = getattr(
                evidence_resolver, "resolve_claim_provenance"
            )
            persisted_claim = resolve_claim(str(claim.get("claim_ref") or ""))
            trusted_provenance = resolve_provenance(
                str(claim.get("provenance_record_ref") or "")
            )
            if persisted_claim is None or trusted_provenance is None:
                raise EvidenceIntegrityError("verified_claim_authority_missing")
            if canonical_value(persisted_claim) != canonical_value(claim):
                raise EvidenceIntegrityError("verified_claim_authority_mismatch")
            validate_trusted_claim_provenance_record(trusted_provenance)
            validate_verified_claim_record(
                persisted_claim,
                context_manifest=context_manifest,
                evidence_by_ref=evidence_items,
                trusted_provenance=trusted_provenance,
            )
        except (AttributeError, EvidenceIntegrityError, TypeError, ValueError):
            provenance_complete = False
        traceable = (
            str(claim.get("context_manifest_ref") or "")
            == str(context_manifest.get("manifest_id") or "")
            and bool(evidence_refs)
            and evidence_refs.issubset(resolved_evidence_refs)
            and evidence_refs.issubset(context_refs)
            and bool(claim_results)
            and claim_results.issubset(result_refs)
            and provenance_complete
        )
        if not traceable:
            claims_traceable = False
            issues.append(f"untraceable_verified_claim:{claim.get('claim_ref') or ''}")
    if not result_refs:
        issues.append("missing_clickhouse_result_refs")
    query_issues = {
        issue
        for issue in issues
        if issue.startswith(
            (
                "missing_clickhouse_",
                "failed_clickhouse_",
                "incomplete_clickhouse_",
                "legacy_clickhouse_",
                "fixed_window_",
                "query_authority_",
                "snapshot_",
                "missing_query_snapshot",
            )
        )
    }
    capability_issues = {
        issue
        for issue in issues
        if "capability_binding" in issue or issue == "missing_runtime_authority_resolver"
    }
    runtime_correctness = {
        "all_required_queries_complete": not query_issues,
        "all_capabilities_bound": not capability_issues,
        "all_claims_traceable": claims_traceable,
    }
    return {
        "required": True,
        "real_clickhouse_verified": not issues and all(runtime_correctness.values()),
        "clickhouse_result_refs": sorted(result_refs),
        "observed_datasets": sorted(observed_datasets),
        "required_datasets": list(required_datasets),
        "analysis_context": dict(analysis_context or {}),
        "runtime_correctness": runtime_correctness,
        "issues": sorted(set(issues)),
    }


def _required_query_readiness_policies(
    plan_payload: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    policies: dict[str, tuple[str, ...]] = {}
    issues: list[str] = []
    raw_slots = plan_payload.get("required_input_slots") or ()
    if not isinstance(raw_slots, (list, tuple)):
        return {}, ("required_input_slots_invalid",)
    for slot_index, slot in enumerate(raw_slots):
        if not isinstance(slot, Mapping) or slot.get("required") is not True:
            issues.append(f"required_slot_invalid:{slot_index}")
            continue
        accepted = tuple(
            dict.fromkeys(
                str(item)
                for item in slot.get("accepted_completeness") or ()
                if str(item)
            )
        )
        if not accepted or any(item not in {"complete", "partial"} for item in accepted):
            issues.append(f"accepted_completeness_invalid:{slot_index}")
            continue
        for ref in slot.get("query_contract_refs") or ():
            _merge_query_readiness_policy(policies, str(ref), accepted, issues)
        for ref in slot.get("validation_query_contract_refs") or ():
            _merge_query_readiness_policy(
                policies,
                str(ref),
                ("complete",),
                issues,
            )
    return policies, tuple(dict.fromkeys(issues))


def _merge_query_readiness_policy(
    policies: dict[str, tuple[str, ...]],
    query_ref: str,
    accepted: tuple[str, ...],
    issues: list[str],
) -> None:
    if not query_ref:
        issues.append("query_contract_ref_missing")
        return
    previous = policies.get(query_ref)
    if previous is None:
        policies[query_ref] = accepted
        return
    intersection = tuple(item for item in previous if item in accepted)
    if not intersection:
        issues.append(f"query_readiness_policy_conflict:{query_ref}")
        return
    policies[query_ref] = intersection


def _report_is_contract_accepted(
    report: Mapping[str, Any],
    *,
    accepted_completeness: tuple[str, ...],
    validation_query: bool,
) -> bool:
    status = str(report.get("completeness_status") or "")
    readiness = str(report.get("analysis_readiness") or "")
    assertions = tuple(
        item
        for item in report.get("assertion_results") or ()
        if isinstance(item, Mapping)
    )
    failure_reasons = tuple(report.get("failure_reasons") or ())
    if status not in accepted_completeness:
        return False
    if validation_query or status == "complete":
        return bool(
            status == "complete"
            and readiness == "ready"
            and assertions
            and not failure_reasons
            and all(item.get("passed") is True for item in assertions)
        )
    execution_assertions = tuple(
        item
        for item in assertions
        if str(item.get("assertion") or "") == "execution_succeeded"
    )
    return bool(
        status == "partial"
        and readiness == "degraded"
        and len(execution_assertions) == 1
        and execution_assertions[0].get("passed") is True
    )


def _runtime_audit_package(result: Mapping[str, Any]) -> dict[str, Any]:
    client_package = result.get("answer_package") or {}
    if not isinstance(client_package, Mapping):
        client_package = {}
    raw_path = result.get("artifact_path") or client_package.get("artifact_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return dict(client_package)
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return dict(client_package)
    if not isinstance(payload, Mapping):
        return dict(client_package)
    expected_run_id = str(result.get("run_id") or client_package.get("run_id") or "")
    if expected_run_id and str(payload.get("run_id") or "") != expected_run_id:
        return dict(client_package)
    return dict(payload)


def _clickhouse_query_intent_issues(answer_package: dict[str, Any]) -> list[str]:
    admin = answer_package.get("admin_audit") or {}
    if not isinstance(admin, dict):
        return []
    row_query_plan = admin.get("row_query_plan") or {}
    if not isinstance(row_query_plan, dict):
        return []
    query_plans = row_query_plan.get("query_plans") or ()
    expected: list[str] = []
    if isinstance(query_plans, (list, tuple)):
        for item in query_plans:
            if not isinstance(item, dict):
                continue
            if item.get("reason") or not item.get("sql_text"):
                continue
            intent = str(item.get("query_intent") or item.get("intent") or "")
            if intent and intent != "dimension_scan_reuse":
                expected.append(intent)
    if not expected:
        return []
    query_results = row_query_plan.get("query_results") or ()
    actual_results = {
        str(item.get("intent") or item.get("query_intent") or "")
        for item in query_results
        if isinstance(item, dict)
    }
    refs_by_intent = row_query_plan.get("result_refs_by_intent") or {}
    if not isinstance(refs_by_intent, dict):
        refs_by_intent = {}
    issues = []
    for intent in dict.fromkeys(expected):
        refs = refs_by_intent.get(intent) or ()
        if intent not in actual_results or not refs:
            issues.append(f"missing_clickhouse_query_intent:{intent}")
    return issues


def _clickhouse_result_refs(answer_package: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    if not isinstance(answer_package, dict):
        return refs
    for section in answer_package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            refs.extend(str(ref) for ref in item.get("result_refs", []) if ref)
    return refs


def _looks_like_clickhouse_result_ref(ref: str) -> bool:
    return bool(ref) and ref != "fixture-hash" and not ref.startswith("phase4-draft")


def _clickhouse_runtime_validator_passed(answer_package: dict[str, Any]) -> bool:
    if not isinstance(answer_package, dict):
        return False
    admin = answer_package.get("admin_audit") or {}
    if not isinstance(admin, dict):
        return False
    for item in admin.get("validator_results", []):
        if not isinstance(item, dict):
            continue
        if (
            item.get("validator") == "clickhouse_runtime"
            and item.get("ok") is True
            and item.get("reason") == "provider_rows_loaded"
        ):
            return True
    return False


def _claim_evidence_review(
    answer_package: dict[str, Any],
    context_manifest: dict[str, Any],
    *,
    requires_claims: bool,
) -> dict[str, Any]:
    claims = _claims(answer_package)
    traceable_refs = _traceable_refs(answer_package, context_manifest)
    manifest_id = str(context_manifest.get("manifest_id") or "")
    missing_claim_refs: list[int] = []
    missing_context_manifest_ref: list[int] = []
    missing_reuse_decision_indexes: list[int] = []
    unsupported_refs: list[str] = []
    for index, claim in enumerate(claims):
        if str(claim.get("context_manifest_ref") or "") != manifest_id:
            missing_context_manifest_ref.append(index)
        reuse = claim.get("reuse_decisions")
        if not isinstance(reuse, list) or not reuse:
            missing_reuse_decision_indexes.append(index)
        refs = [str(ref) for ref in claim.get("evidence_refs", []) if ref]
        if not refs:
            missing_claim_refs.append(index)
        for ref in refs:
            if ref not in traceable_refs:
                unsupported_refs.append(ref)
    return {
        "claim_count": len(claims),
        "traceable_refs": sorted(traceable_refs),
        "missing_claim_ref_indexes": missing_claim_refs,
        "missing_context_manifest_ref": missing_context_manifest_ref,
        "missing_reuse_decision_indexes": missing_reuse_decision_indexes,
        "unsupported_evidence_refs": sorted(set(unsupported_refs)),
        "passed": (
            (not requires_claims or bool(claims))
            and not missing_claim_refs
            and not missing_context_manifest_ref
            and not missing_reuse_decision_indexes
            and not unsupported_refs
        ),
    }


def _claims(answer_package: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for section in answer_package.get("sections", []):
        payload = section.get("payload", {}) if isinstance(section, dict) else {}
        section_claims = payload.get("claims")
        if isinstance(section_claims, list):
            claims.extend(claim for claim in section_claims if isinstance(claim, dict))
    return claims


def _traceable_refs(answer_package: dict[str, Any], context_manifest: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for item in context_manifest.get("items", []):
        source_ref = str(item.get("source_ref", "")) if isinstance(item, Mapping) else ""
        source_type = str(item.get("source_type", "")) if isinstance(item, Mapping) else ""
        if (
            isinstance(item, Mapping)
            and source_ref
            and (
                source_type in {"evidence", "result", "artifact", "memory"}
                or source_ref.startswith(("evidence:", "result:", "artifact:", "memory:"))
            )
            and item.get("can_support_claims") is True
            and item.get("claim_use") not in {"context_only", "preference_only", "blocked"}
        ):
            refs.add(source_ref)
    for item in context_manifest.get("sources", []):
        source_ref = str(item.get("ref", "")) if isinstance(item, Mapping) else ""
        source_type = str(item.get("type", "")) if isinstance(item, Mapping) else ""
        if (
            isinstance(item, Mapping)
            and source_ref
            and source_type in {"evidence", "result", "completeness", "artifact", "memory"}
            and item.get("can_support_claim") is True
        ):
            refs.add(source_ref)
    return refs


def _missing_inputs_from_error(exc: Exception, *, real_llm: bool = False, real_clickhouse: bool = False) -> list[str]:
    text = str(exc)
    missing: list[str] = []
    if "WAJE_RUNTIME_DATABASE_URL or DATABASE_URL" in text:
        missing.extend(["WAJE_RUNTIME_DATABASE_URL", "DATABASE_URL"])
    if real_llm:
        if not os.environ.get("WAJE_LLM_MODEL"):
            missing.append("WAJE_LLM_MODEL")
        if not (
            os.environ.get("WAJE_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
        ):
            missing.extend(["WAJE_LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"])
    if real_clickhouse:
        for key in (
            "WAJE_CLICKHOUSE_HOST",
            "WAJE_CLICKHOUSE_PORT",
            "WAJE_CLICKHOUSE_USER",
            "WAJE_CLICKHOUSE_PASSWORD",
            "WAJE_CLICKHOUSE_DATABASE",
            "WAJE_CLICKHOUSE_SECURE",
        ):
            if not os.environ.get(key):
                missing.append(key)
    return list(dict.fromkeys(missing))


def _case_thread_id(case: dict[str, Any]) -> str:
    return f"live-{case['id']}-{uuid4().hex[:8]}"


def _run_mode(*, real_llm: bool, real_clickhouse: bool) -> str:
    if real_llm and real_clickhouse:
        return "real_llm_real_clickhouse"
    if real_llm:
        return "real_llm"
    if real_clickhouse:
        return "real_clickhouse"
    return "dry_run"


def _default_artifact_dir(*, real_llm: bool, real_clickhouse: bool) -> Path:
    suffix = "real" if real_llm or real_clickhouse else "dry-run"
    return Path(f"artifacts/phase7/live-conversation-{suffix}")


def _aggregate_real_clickhouse_review(
    turns: list[dict[str, Any]],
    real_clickhouse: bool,
    required_datasets: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    refs: list[str] = []
    datasets: set[str] = set()
    issues: list[str] = []
    verified = True
    runtime_correctness = {
        key: True
        for key in (
            "all_required_queries_complete",
            "all_capabilities_bound",
            "all_claims_traceable",
        )
    }
    for turn in turns:
        review = turn.get("real_clickhouse_review") or {}
        refs.extend(str(ref) for ref in review.get("clickhouse_result_refs", []) if ref)
        datasets.update(
            str(dataset)
            for dataset in review.get("observed_datasets", [])
            if dataset
        )
        issues.extend(str(issue) for issue in review.get("issues", []) if issue)
        if review.get("real_clickhouse_verified") is not True:
            verified = False
        turn_correctness = review.get("runtime_correctness") or {}
        for key in runtime_correctness:
            if turn_correctness.get(key) is not True:
                runtime_correctness[key] = False
    if not real_clickhouse:
        verified = True
        issues = []
    else:
        for dataset in required_datasets:
            if dataset not in datasets:
                issues.append(f"missing_required_dataset:{dataset}")
                verified = False
                runtime_correctness["all_required_queries_complete"] = False
        if not all(runtime_correctness.values()) or issues:
            verified = False
    return {
        "required": bool(real_clickhouse),
        "real_clickhouse_verified": verified,
        "clickhouse_result_refs": sorted(set(refs)),
        "observed_datasets": sorted(datasets),
        "required_datasets": list(required_datasets),
        "runtime_correctness": runtime_correctness,
        "issues": sorted(set(issues)),
    }


def _case_output(
    *,
    case: dict[str, Any],
    thread_id: str,
    run_mode: str,
    strict_quality: bool,
    real_clickhouse: bool,
    turns: list[dict[str, Any]],
    status: str | None = None,
) -> dict[str, Any]:
    final_result = _effective_result(turns[-1]) if turns else {}
    expectation_failed = any(not turn["expectation_review"]["passed"] for turn in turns)
    strict_quality_failed = any(turn.get("strict_quality_failed") for turn in turns)
    real_clickhouse_review = _aggregate_real_clickhouse_review(
        turns,
        real_clickhouse,
        case.get("required_datasets") or (),
    )
    real_clickhouse_failed = not real_clickhouse_review["real_clickhouse_verified"]
    quality_warnings = sorted(
        {
            str(warning)
            for turn in turns
            for warning in (
                (
                    (turn.get("resumed_quality_review") or turn.get("quality_review") or {})
                    .get("quality_warnings")
                    or ()
                )
            )
            if warning
        }
    )
    return {
        "case_id": case["id"],
        "analysis_context": dict(case.get("analysis_context") or {}),
        "required_datasets": list(case.get("required_datasets") or ()),
        "thread_id": thread_id,
        "run_mode": run_mode,
        "status": status
        or (
            "failed"
            if expectation_failed or strict_quality_failed or real_clickhouse_failed
            else "passed"
        ),
        "strict_quality": strict_quality,
        "strict_quality_failed": strict_quality_failed,
        "quality_warnings": quality_warnings,
        "quality_warning_count": len(quality_warnings),
        "real_clickhouse_review": real_clickhouse_review,
        "real_clickhouse_verified": real_clickhouse_review["real_clickhouse_verified"],
        "clickhouse_result_refs": real_clickhouse_review["clickhouse_result_refs"],
        "final_turn_status": final_result.get("status"),
        "run_id": final_result.get("run_id"),
        "topic_id": final_result.get("topic_id"),
        "answer_package": final_result.get("answer_package"),
        "context_manifest": final_result.get("context_manifest"),
        "accepted_graph": final_result.get("accepted_graph") or [],
        "llm_calls": final_result.get("llm_calls", []),
        "quality_review": final_result.get("quality_review"),
        "turns": turns,
    }


def _write_case_artifact(
    artifact_dir: Path,
    case_id: str,
    output: dict[str, Any],
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / f"{case_id}.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_case(
    core: ConversationAgentCore,
    case: dict[str, Any],
    artifact_dir: Path,
    *,
    strict_quality: bool = False,
    real_clickhouse: bool = False,
    run_mode: str = "dry_run",
) -> dict[str, Any]:
    thread_id = _case_thread_id(case)
    analysis_context = dict(case.get("analysis_context") or {})
    required_datasets = tuple(case.get("required_datasets") or ())
    turns: list[dict[str, Any]] = []
    for index, turn in enumerate(case["turns"], start=1):
        result = core.run_message(
            thread_id=thread_id,
            user_message=turn["user"],
            analysis_context=analysis_context or None,
        )
        answer_package = result.get("answer_package") or {}
        turn_record = {
            "index": index,
            "user": turn["user"],
            "status": result["status"],
            "run_id": result["run_id"],
            "topic_id": result.get("topic_id"),
            "intent": result.get("intent"),
            "topic_relation": result.get("topic_relation"),
            "failure_reason": result.get("failure_reason"),
            "answer_package": result.get("answer_package"),
            "context_manifest": result.get("context_manifest"),
            "accepted_graph": result.get("accepted_graph"),
            "llm_calls": result.get("llm_calls", []),
            "quality_review": _quality_review(answer_package),
            "clarification": result.get("clarification"),
            "artifact_path": result.get("artifact_path"),
        }
        current = result
        clarification_resumes: list[dict[str, Any]] = []
        configured_response = str(turn.get("clarification_response") or "").strip()
        for clarification_index in range(1, 9):
            if current["status"] != "waiting_for_clarification":
                break
            response = configured_response if clarification_index == 1 else ""
            response = response or _automatic_clarification_response(current)
            resumed = core.run_message(
                thread_id=thread_id,
                user_message=response,
                analysis_context=analysis_context or None,
            )
            resumed_answer_package = resumed.get("answer_package") or {}
            clarification_resumes.append({
                "index": clarification_index,
                "response": response,
                "status": resumed["status"],
                "run_id": resumed["run_id"],
                "topic_id": resumed.get("topic_id"),
                "failure_reason": resumed.get("failure_reason"),
                "clarification": resumed.get("clarification"),
            })
            turn_record["clarification_response"] = response
            turn_record["resumed_status"] = resumed["status"]
            turn_record["resumed_run_id"] = resumed["run_id"]
            turn_record["resumed_topic_id"] = resumed.get("topic_id")
            turn_record["resumed_intent"] = resumed.get("intent")
            turn_record["resumed_topic_relation"] = resumed.get("topic_relation")
            turn_record["resumed_failure_reason"] = resumed.get("failure_reason")
            turn_record["resumed_answer_package"] = resumed.get("answer_package")
            turn_record["resumed_context_manifest"] = resumed.get("context_manifest")
            turn_record["resumed_accepted_graph"] = resumed.get("accepted_graph")
            turn_record["resumed_llm_calls"] = resumed.get("llm_calls", [])
            turn_record["resumed_quality_review"] = _quality_review(resumed_answer_package)
            turn_record["resumed_clarification"] = resumed.get("clarification")
            turn_record["resumed_artifact_path"] = resumed.get("artifact_path")
            current = resumed
        if clarification_resumes:
            turn_record["clarification_resumes"] = clarification_resumes
        turn_record["expectation_review"] = _review_expectations(turn, turn_record)
        effective = _effective_result(turn_record)
        turn_record["real_clickhouse_review"] = _real_clickhouse_review(
            effective,
            real_clickhouse=real_clickhouse,
            evidence_resolver=getattr(core, "evidence_resolver", None),
            required_datasets=required_datasets,
            analysis_context=analysis_context,
        )
        turn_record["strict_quality_failed"] = bool(
            strict_quality and _strict_quality_failed(turn_record)
        )
        turns.append(turn_record)
        _write_case_artifact(
            artifact_dir,
            case["id"],
            _case_output(
                case=case,
                thread_id=thread_id,
                run_mode=run_mode,
                strict_quality=strict_quality,
                real_clickhouse=real_clickhouse,
                turns=turns,
                status="running",
            ),
        )
    output = _case_output(
        case=case,
        thread_id=thread_id,
        run_mode=run_mode,
        strict_quality=strict_quality,
        real_clickhouse=real_clickhouse,
        turns=turns,
    )
    _write_case_artifact(artifact_dir, case["id"], output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="evals/phase7/conversation_scenarios.yaml")
    parser.add_argument("--case")
    parser.add_argument("--artifact-dir")
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--real-clickhouse", action="store_true")
    parser.add_argument("--strict-quality", action="store_true")
    args = parser.parse_args()

    load_env_file()
    run_mode = _run_mode(real_llm=args.real_llm, real_clickhouse=args.real_clickhouse)
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else _default_artifact_dir(
        real_llm=args.real_llm,
        real_clickhouse=args.real_clickhouse,
    )
    selected = select_cases(load_cases(args.cases), args.case)
    try:
        core = ConversationAgentCore.from_environment(
            real_llm=args.real_llm,
            real_clickhouse=args.real_clickhouse,
        )
        results = [
            run_case(
                core,
                case,
                artifact_dir,
                strict_quality=args.strict_quality,
                real_clickhouse=args.real_clickhouse,
                run_mode=run_mode,
            )
            for case in selected
        ]
    except RuntimeError as exc:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_name = f"{args.case}.json" if args.case else "environment_blocked.json"
        case_id = args.case or "environment_blocked"
        blocked = {
            "case_id": case_id,
            "run_mode": run_mode,
            "status": "blocked",
            "final_turn_status": "blocked",
            "run_id": None,
            "topic_id": None,
            "answer_package": None,
            "context_manifest": None,
            "accepted_graph": [],
            "llm_calls": [],
            "quality_review": None,
            "strict_quality": args.strict_quality,
            "strict_quality_failed": None,
            "turns": [],
            "missing_inputs": _missing_inputs_from_error(
                exc,
                real_llm=args.real_llm,
                real_clickhouse=args.real_clickhouse,
            ),
            "owner": "local runtime/deployment owner",
            "error": str(exc),
        }
        (artifact_dir / artifact_name).write_text(
            json.dumps(blocked, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
    print(
        json.dumps(
            {"case_count": len(results), "case_ids": [case["case_id"] for case in results]},
            ensure_ascii=False,
        )
    )
    if any(result.get("status") != "passed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
