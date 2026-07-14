from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.models import validate_result_reuse_candidate
from bi_agent.runtime.analysis_obligations import (
    ObligationRequest,
    ObligationResolution,
    resolve_analysis_obligations,
)
from bi_agent.runtime.analysis_contracts import (
    CompletenessReport,
    DimensionBinding,
    JoinExpectation,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ReconciliationBinding,
    ResolvedWindow,
    ResultShape,
    analysis_contract_signature,
    analysis_contract_from_dict,
    query_contract_signature,
)
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
    validate_capability_plan_semantics,
)
from bi_agent.runtime.claim_provenance import (
    validate_trusted_claim_provenance_record,
    validate_verified_claim_record,
)
from bi_agent.runtime.coverage_audit import audit_existing_data_coverage
from bi_agent.runtime.evidence_authority import (
    CapabilityBindingRecord,
    EvidenceIntegrityError,
    QueryExecutionRecord,
    RowsPayloadLoader,
    RuntimeEvidenceResolver,
    canonical_digest,
    canonical_value,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.runtime_publication_index import (
    RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION,
    RUNTIME_PUBLICATION_RECORD_GROUPS,
    runtime_publication_record_ref,
)
from bi_agent.runtime.query_completeness import CURRENT_DATA_ASSERTIONS


def load_cases(path: str) -> list[dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return []
    defaults = raw.get("analysis_context_defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ValueError("eval_analysis_context_defaults_invalid")
    cases = raw.get("conversation_cases", [])
    loaded = [deepcopy(case) for case in cases if isinstance(case, dict) and case.get("id")]
    for case in loaded:
        if defaults and not case.get("analysis_context"):
            case["analysis_context"] = dict(defaults)
    return loaded


def select_cases(cases: list[dict[str, Any]], case_id: str | None) -> list[dict[str, Any]]:
    if not case_id:
        return cases
    return [case for case in cases if case["id"] == case_id]


def load_suite_cases(suite: str) -> list[dict[str, Any]]:
    if suite == "fixed-eight":
        return select_cases(
            load_cases("evals/phase7/conversation_scenarios.yaml"),
            "paid_amount_revenue_diagnostics_8_question_set",
        )
    if suite == "platform-current-data":
        return load_cases("evals/phase7/existing_data_coverage_scenarios.yaml")
    raise ValueError(f"unknown_eval_suite:{suite}")


def resolve_cli_cases(
    suite: str | None,
    cases_path: str | None,
    case_id: str | None,
) -> list[dict[str, Any]]:
    if suite and cases_path:
        raise ValueError("eval_cli_source_conflict")
    if cases_path:
        cases = load_cases(cases_path)
        selected = select_cases(cases, case_id)
        if case_id and not selected:
            raise ValueError("eval_case_unknown")
    else:
        cases = load_suite_cases(suite or "fixed-eight")
        selected = select_cases(cases, case_id)
        if case_id and not selected:
            raise ValueError("eval_case_not_in_suite")
    if not selected:
        raise ValueError("eval_case_selection_empty")
    return selected


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
        "failure_reason": turn_record.get("failure_reason"),
        "answer_package": turn_record.get("answer_package"),
        "context_manifest": turn_record.get("context_manifest"),
        "accepted_graph": turn_record.get("accepted_graph") or [],
        "llm_calls": turn_record.get("llm_calls", []),
        "quality_review": turn_record.get("quality_review"),
        "artifact_path": turn_record.get("artifact_path"),
    }


_RUN_FAILURE_EVALUATION_STATUS = "not_evaluated_due_to_run_failure"
_RUNTIME_CORRECTNESS_KEYS = (
    "all_required_queries_complete",
    "all_capabilities_bound",
    "all_claims_traceable",
)


def _run_failure_evaluation(
    turn_record: Mapping[str, Any],
) -> dict[str, Any] | None:
    if turn_record.get("resumed_status") == "failed":
        stage = "clarification_resume"
        run_id = turn_record.get("resumed_run_id")
        reason = turn_record.get("resumed_failure_reason")
    elif turn_record.get("status") == "failed":
        stage = "run"
        run_id = turn_record.get("run_id")
        reason = turn_record.get("failure_reason")
    else:
        return None
    primary_reason = str(reason or "").strip() or "primary_failure_reason_missing"
    return {
        "status": _RUN_FAILURE_EVALUATION_STATUS,
        "evaluated": False,
        "primary_failure": {
            "stage": stage,
            "run_id": run_id,
            "reason": primary_reason,
        },
    }


def _turn_failure_evaluation(
    turn_record: Mapping[str, Any],
) -> dict[str, Any] | None:
    evaluation = turn_record.get("evaluation")
    if (
        isinstance(evaluation, Mapping)
        and evaluation.get("status") == _RUN_FAILURE_EVALUATION_STATUS
        and evaluation.get("evaluated") is False
        and isinstance(evaluation.get("primary_failure"), Mapping)
    ):
        return dict(evaluation)
    return _run_failure_evaluation(turn_record)


def _evaluated_turns(
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [turn for turn in turns if _turn_failure_evaluation(turn) is None]


def _failed_run_clickhouse_review(
    evaluation: Mapping[str, Any],
    *,
    real_clickhouse: bool,
    required_datasets: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    return {
        **dict(evaluation),
        "required": bool(real_clickhouse),
        "real_clickhouse_verified": None,
        "clickhouse_result_refs": [],
        "observed_datasets": [],
        "required_datasets": list(required_datasets),
        "runtime_correctness": {
            key: None for key in _RUNTIME_CORRECTNESS_KEYS
        },
        "issues": [],
    }


def _effective_quality_review(turn_record: Mapping[str, Any]) -> dict[str, Any]:
    review = _effective_result(dict(turn_record)).get("quality_review")
    return review if isinstance(review, dict) else {}


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


def review_case_obligations(
    turn_record: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    *,
    coverage_authority: Mapping[str, Any] | None = None,
    evidence_resolver: Any = None,
    rows_loader: Any = None,
    release_resolver: Any = None,
    conversation_store: Any = None,
    case_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Review executable obligations without constraining answer wording."""
    scenario = turn_record.get("scenario") or {}
    if not isinstance(scenario, Mapping):
        raise ValueError("scenario_expectation_invalid")
    authored_family = str(scenario.get("question_family") or "")
    authority = turn_record.get("runtime_authority") or {}
    if not isinstance(authority, Mapping):
        raise ValueError("runtime_authority_invalid")
    families, family_authority_status = _persisted_question_family_authority(
        authority,
        registry,
        authored_family=authored_family,
    )
    family = families[0] if families else ""
    request = ObligationRequest(
        question_families=families,
        diagnostic_tags=tuple(scenario.get("diagnostic_tags") or ()),
        target_metrics=tuple(scenario.get("target_metrics") or ()),
        requested_dimensions=tuple(scenario.get("requested_dimensions") or ()),
        baselines=tuple(scenario.get("baselines") or ()),
        context_sources=tuple(scenario.get("context_sources") or ()),
        claim_intents=tuple(scenario.get("claim_intents") or ()),
    )
    resolution, capability_family_provenance = _resolve_family_set_obligations(
        request, registry
    )
    required = tuple(
        dict.fromkeys(
            (*resolution.required_capabilities,
             *resolution.conditional_capabilities,
             *resolution.independent_capabilities)
        )
    )
    authored_required = tuple(
        dict.fromkeys(str(item) for item in scenario.get("required_capabilities") or ())
    )
    actual = {
        str(item)
        for item in turn_record.get("accepted_graph") or ()
        if str(item)
    }
    missing_capabilities = [item for item in required if item not in actual]
    authored_expected_states = scenario.get("expected_dataset_states") or {}
    if not isinstance(authored_expected_states, Mapping):
        raise ValueError("dataset_state_expectation_invalid")
    expected_states = dict(authored_expected_states)
    authored_capability_states = scenario.get("expected_capability_states") or {}
    if not isinstance(authored_capability_states, Mapping):
        raise ValueError("capability_state_expectation_invalid")
    expected_capability_states = {
        str(capability_id): str(state)
        for capability_id, state in authored_capability_states.items()
    }
    unresolved_authority_capabilities: list[str] = []
    ambiguous_authority_capabilities: list[str] = []
    unresolved_authority_roles: list[str] = []
    ambiguous_authority_roles: list[str] = []
    authored_authority_mismatches: list[str] = []
    if coverage_authority is not None:
        expected_states, unresolved_authority_roles, ambiguous_authority_roles = (
            _authority_resolved_dataset_states(
                authored_expected_states,
                required=required,
                question_families=families,
                coverage_authority=coverage_authority,
            )
        )
        authored_authority_mismatches = [
            f"{dataset_id}:{authored_expected_states[dataset_id]}->{state}"
            for dataset_id, state in expected_states.items()
            if authored_expected_states.get(dataset_id) != state
        ]
        (
            expected_capability_states,
            unresolved_authority_capabilities,
            ambiguous_authority_capabilities,
        ) = _authority_resolved_capability_states(
            authored_capability_states,
            dataset_roles=tuple(str(item) for item in authored_expected_states),
            question_families=families,
            coverage_authority=coverage_authority,
        )
    observed_states, observed_gaps = _derive_runtime_dataset_states(
        authority, registry=registry
    )
    capability_outcomes = _derive_capability_outcomes(
        required,
        accepted_capabilities=actual,
        authority=authority,
        registry=registry,
    )
    nonterminal_capabilities = [
        capability_id
        for capability_id, outcome in capability_outcomes.items()
        if outcome not in {"executed", "degraded", "blocked"}
    ]
    capability_state_mismatches = [
        f"{capability_id}:{expected_state}->{capability_outcomes.get(capability_id, 'missing')}"
        for capability_id, expected_state in expected_capability_states.items()
        if (
            expected_state == "executable"
            and capability_outcomes.get(capability_id) != "executed"
        )
        or (
            expected_state != "executable"
            and capability_outcomes.get(capability_id) not in {"degraded", "blocked"}
        )
    ]
    capability_authority_gate = bool(
        coverage_authority is not None and expected_capability_states
    )
    observed_capability_dataset_states = _capability_dataset_observations(
        expected_capability_states,
        capability_outcomes,
        coverage_authority,
    )
    authored_excluded = scenario.get("excluded_inputs") or {}
    if not isinstance(authored_excluded, Mapping):
        raise ValueError("excluded_input_expectation_invalid")
    expected_gaps = dict(authored_excluded)
    if coverage_authority is not None:
        expected_gaps = _authority_resolved_expected_gaps(
            authored_excluded, expected_states
        )
    missing_data = [
        f"{dataset_id}:{state}"
        for dataset_id, state in expected_states.items()
        if not _legacy_dataset_state_satisfies(
            expected=state,
            observed=observed_states.get(dataset_id, "unobserved"),
        )
    ]
    mismatched_gaps = [
        f"{dataset_id}:{gap_type}"
        for dataset_id, gap_type in authored_excluded.items()
        if dataset_id not in expected_states
    ]
    missing_expected_gaps = [
        f"{dataset_id}:{gap_type}"
        for dataset_id, gap_type in expected_gaps.items()
        if not _gap_expectation_matches(
            str(gap_type), observed_gaps.get(str(dataset_id), ())
        )
    ]
    claim_review = _review_claim_ceiling(
        authority,
        scenario,
        registry,
        evidence_resolver=evidence_resolver,
        rows_loader=rows_loader,
        release_resolver=release_resolver,
    )
    authored_terminal_boundary = str(scenario.get("terminal_boundary") or "")
    resolved_terminal_boundary = (
        _authority_resolved_terminal_boundary(
            authored_terminal_boundary, expected_capability_states
        )
        if capability_authority_gate
        else _authority_resolved_terminal_boundary(
            authored_terminal_boundary, expected_states
        )
    )
    terminal_scenario = dict(scenario)
    terminal_scenario["terminal_boundary"] = resolved_terminal_boundary
    terminal_review = _review_terminal_boundary(
        turn_record,
        terminal_scenario,
        observed_states,
    )
    clarification_required = scenario.get("clarification_resume") == "required"
    clarification_passed = (
        not clarification_required
        or turn_record.get("resumed_status") == "completed"
        and turn_record.get("resumed_topic_id") == turn_record.get("topic_id")
    )
    reuse_required = scenario.get("reuse") == "required"
    reuse_review = (
        _review_required_reuse(
            authority,
            scenario.get("expected_reuse") or {},
            registry=registry,
            evidence_resolver=evidence_resolver,
            rows_loader=rows_loader,
            release_resolver=release_resolver,
            conversation_store=conversation_store,
            case_lineage=case_lineage,
        )
        if reuse_required
        else {
            "passed": True,
            "errors": [],
            "source_result_ref": "",
            "current_result_ref": "",
            "query_contract_ref": "",
            "capability_id": "",
            "dataset_ids": [],
        }
    )
    reuse_passed = (
        not reuse_required
        or bool(turn_record.get("prior_topic_id"))
        and turn_record.get("topic_id") == turn_record.get("prior_topic_id")
        and reuse_review["passed"] is True
    )
    hard_passed = (
        family_authority_status in {"matched", "mismatch"}
        and not nonterminal_capabilities
        and not capability_state_mismatches
        and not unresolved_authority_capabilities
        and (capability_authority_gate or not unresolved_authority_roles)
        and (capability_authority_gate or not missing_data)
        and not mismatched_gaps
        and (capability_authority_gate or not missing_expected_gaps)
        and claim_review["passed"]
        and terminal_review["passed"]
        and clarification_passed
        and reuse_passed
    )
    return {
        "authored_question_family": authored_family,
        "question_family": family,
        "question_families": list(families),
        "question_family_authority_status": family_authority_status,
        "question_family_mismatch": family_authority_status == "mismatch",
        "capability_family_provenance": {
            capability_id: list(source_families)
            for capability_id, source_families in capability_family_provenance.items()
        },
        "authored_required_capabilities": list(authored_required),
        "authored_required_capability_mismatches": [
            capability_id
            for capability_id in authored_required
            if capability_id not in required
        ],
        "required_capability_authority_diff": {
            "authored_only": [
                capability_id
                for capability_id in authored_required
                if capability_id not in required
            ],
            "derived_only": [
                capability_id
                for capability_id in required
                if capability_id not in authored_required
            ],
        },
        "required_capabilities": list(required),
        "conditional_capabilities": list(resolution.conditional_capabilities),
        "independent_capabilities": list(resolution.independent_capabilities),
        "minimum_publishable_evidence": list(resolution.minimum_publishable_evidence),
        "allowed_claim_ceiling": scenario.get("allowed_claim_ceiling"),
        "authored_terminal_boundary": scenario.get("terminal_boundary"),
        "terminal_boundary": resolved_terminal_boundary,
        "missing_required_capabilities": missing_capabilities,
        "capability_outcomes": capability_outcomes,
        "nonterminal_required_capabilities": nonterminal_capabilities,
        "authored_expected_capability_states": dict(authored_capability_states),
        "expected_capability_states": expected_capability_states,
        "capability_state_mismatches": capability_state_mismatches,
        "observed_capability_dataset_states": observed_capability_dataset_states,
        "dataset_obligation_gate_mode": (
            "capability_authority"
            if capability_authority_gate
            else "dataset_legacy_fallback"
        ),
        "unresolved_authority_capabilities": unresolved_authority_capabilities,
        "ambiguous_authority_capabilities": ambiguous_authority_capabilities,
        "authored_expected_dataset_states": dict(authored_expected_states),
        "expected_dataset_states": dict(expected_states),
        "authored_authority_mismatches": authored_authority_mismatches,
        "unresolved_authority_dataset_roles": unresolved_authority_roles,
        "ambiguous_authority_dataset_roles": ambiguous_authority_roles,
        "observed_dataset_states": dict(observed_states),
        "observed_typed_gaps": {
            dataset_id: list(gaps) for dataset_id, gaps in observed_gaps.items()
        },
        "authored_expected_typed_gaps": dict(authored_excluded),
        "expected_typed_gaps": dict(expected_gaps),
        "missing_current_data_obligations": missing_data,
        "invalid_typed_gaps": mismatched_gaps,
        "missing_expected_typed_gaps": missing_expected_gaps,
        "actual_max_claim_strength": claim_review["actual_max_claim_strength"],
        "actual_authority_ceiling": claim_review["actual_authority_ceiling"],
        "claim_authority_reviews": claim_review["claim_authority_reviews"],
        "missing_claim_capability_provenance": claim_review[
            "missing_claim_capability_provenance"
        ],
        "claim_ceiling_passed": claim_review["passed"],
        "terminal_outcome": terminal_review["outcome"],
        "terminal_boundary_passed": terminal_review["passed"],
        "clarification_resume_passed": clarification_passed,
        "reuse_review": reuse_review,
        "reuse_passed": reuse_passed,
        "hard_acceptance_passed": hard_passed,
    }


def _persisted_question_family_authority(
    authority: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    *,
    authored_family: str,
) -> tuple[tuple[str, ...], str]:
    """Resolve the ordered canonical family set from persisted authority."""
    authority_error = str(authority.get("_authority_error") or "")
    if authority_error:
        return (), authority_error
    admin = authority.get("admin_audit") or authority
    if not isinstance(admin, Mapping):
        return (), "missing"
    raw_contract = admin.get("analysis_contract")
    if not isinstance(raw_contract, Mapping):
        return (), "missing"
    try:
        contract = analysis_contract_from_dict(raw_contract)
    except (KeyError, TypeError, ValueError):
        return (), "invalid_contract"
    families = contract.question_families
    if not families:
        return (), "missing"
    if len(set(families)) != len(families):
        return (), "invalid_contract"
    if any(family not in registry.question_family_ids for family in families):
        return (), "invalid"
    return families, "matched" if authored_family in families else "mismatch"


def _resolve_family_set_obligations(
    request: ObligationRequest,
    registry: RuntimeContractRegistry,
) -> tuple[ObligationResolution, dict[str, tuple[str, ...]]]:
    required: list[str] = []
    conditional: list[str] = []
    independent: list[str] = []
    evidence: list[str] = []
    provenance: dict[str, list[str]] = {}
    for family in request.question_families:
        supported_tags = tuple(
            tag
            for tag in request.diagnostic_tags
            if family
            in set(registry.diagnostic_obligation(tag)["supported_question_families"])
        )
        family_resolution = resolve_analysis_obligations(
            ObligationRequest(
                question_families=(family,),
                diagnostic_tags=supported_tags,
                target_metrics=request.target_metrics,
                requested_dimensions=request.requested_dimensions,
                baselines=request.baselines,
                context_sources=request.context_sources,
                claim_intents=request.claim_intents,
            ),
            registry,
        )
        required.extend(family_resolution.required_capabilities)
        conditional.extend(family_resolution.conditional_capabilities)
        independent.extend(family_resolution.independent_capabilities)
        evidence.extend(family_resolution.minimum_publishable_evidence)
        for capability_id in (
            *family_resolution.required_capabilities,
            *family_resolution.conditional_capabilities,
            *family_resolution.independent_capabilities,
        ):
            provenance.setdefault(capability_id, []).append(family)
    ordered_required = registry.order_capabilities(required)
    required_set = set(ordered_required)
    ordered_conditional = registry.order_capabilities(
        item for item in conditional if item not in required_set
    )
    conditional_set = set(ordered_conditional)
    ordered_independent = registry.order_capabilities(
        item
        for item in independent
        if item not in required_set and item not in conditional_set
    )
    resolution = ObligationResolution(
        required_capabilities=ordered_required,
        conditional_capabilities=ordered_conditional,
        independent_capabilities=ordered_independent,
        minimum_publishable_evidence=tuple(dict.fromkeys(evidence)),
        mutations=tuple(
            {"action": "obligation_required", "capability": capability_id}
            for capability_id in (*ordered_required, *ordered_conditional)
        ),
    )
    return resolution, {
        capability_id: tuple(dict.fromkeys(source_families))
        for capability_id, source_families in provenance.items()
    }


def _capability_dataset_observations(
    expected_states: Mapping[str, str],
    outcomes: Mapping[str, str],
    coverage_authority: Mapping[str, Any] | None,
) -> dict[str, list[dict[str, str]]]:
    if coverage_authority is None:
        return {}
    cells = coverage_authority.get("cells") or {}
    if not isinstance(cells, Mapping):
        return {}
    observations: dict[str, list[dict[str, str]]] = {}
    for cell_id, cell in sorted(cells.items(), key=lambda item: str(item[0])):
        if not isinstance(cell, Mapping):
            continue
        capability_id = str(cell.get("capability") or "")
        if capability_id not in expected_states:
            continue
        outcome = str(outcomes.get(capability_id) or "")
        observed_state = {
            "executed": "executable",
            "degraded": "degraded",
            "blocked": str(cell.get("state") or "blocked"),
        }.get(outcome, "unobserved")
        for dataset_id in cell.get("datasets") or ():
            observations.setdefault(capability_id, []).append({
                "cell_id": str(cell_id),
                "dataset_id": str(dataset_id),
                "authority_state": str(cell.get("state") or ""),
                "outcome": outcome,
                "observed_state": observed_state,
            })
    return observations


def _authority_resolved_capability_states(
    authored_states: Mapping[str, Any],
    *,
    dataset_roles: tuple[str, ...],
    question_families: tuple[str, ...],
    coverage_authority: Mapping[str, Any],
) -> tuple[dict[str, str], list[str], list[str]]:
    cells = coverage_authority.get("cells") or {}
    if not isinstance(cells, Mapping):
        raise ValueError("coverage_authority_cells_invalid")
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    ambiguous: list[str] = []
    role_set = set(dataset_roles)
    for raw_capability_id in authored_states:
        capability_id = str(raw_capability_id)
        applicable: list[str] = []
        for cell in cells.values():
            if not isinstance(cell, Mapping):
                raise ValueError("coverage_authority_cell_invalid")
            if str(cell.get("capability") or "") != capability_id:
                continue
            state = str(cell.get("state") or "")
            if state not in _DATASET_STATE_PRECEDENCE:
                continue
            datasets = {str(item) for item in cell.get("datasets") or ()}
            families = {str(item) for item in cell.get("question_families") or ()}
            if (not role_set or role_set & datasets) and (
                not families or bool(set(question_families) & families)
            ):
                applicable.append(state)
        if not applicable:
            unresolved.append(capability_id)
            continue
        if len(set(applicable)) > 1:
            ambiguous.append(capability_id)
        resolved[capability_id] = max(
            applicable, key=lambda state: _DATASET_STATE_PRECEDENCE[state]
        )
    return resolved, unresolved, ambiguous


def _authority_resolved_dataset_states(
    authored_states: Mapping[str, Any],
    *,
    required: tuple[str, ...],
    question_families: tuple[str, ...],
    coverage_authority: Mapping[str, Any],
) -> tuple[dict[str, str], list[str], list[str]]:
    cells = coverage_authority.get("cells") or {}
    if not isinstance(cells, Mapping):
        raise ValueError("coverage_authority_cells_invalid")
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    ambiguous: list[str] = []
    required_set = set(required)
    for raw_dataset_id in authored_states:
        dataset_id = str(raw_dataset_id)
        required_states: list[str] = []
        family_states: list[str] = []
        dataset_states: list[str] = []
        for cell in cells.values():
            if not isinstance(cell, Mapping):
                raise ValueError("coverage_authority_cell_invalid")
            datasets = {str(item) for item in cell.get("datasets") or ()}
            capability = str(cell.get("capability") or "")
            families = {str(item) for item in cell.get("question_families") or ()}
            if dataset_id not in datasets:
                continue
            dataset_states.append(str(cell.get("state") or ""))
            if capability in required_set and (
                not families or bool(set(question_families) & families)
            ):
                required_states.append(str(cell.get("state") or ""))
            if set(question_families) & families:
                family_states.append(str(cell.get("state") or ""))
        states = required_states or family_states or dataset_states
        valid = [state for state in states if state in _DATASET_STATE_PRECEDENCE]
        if not valid:
            unresolved.append(dataset_id)
            continue
        if not required_states and not family_states:
            ambiguous.append(dataset_id)
        resolved[dataset_id] = max(
            valid, key=lambda state: _DATASET_STATE_PRECEDENCE[state]
        )
    return resolved, unresolved, ambiguous


def _authority_resolved_terminal_boundary(
    authored_boundary: str,
    expected_states: Mapping[str, str],
) -> str:
    states = set(expected_states.values())
    if "permission_blocked" in states:
        return "permission_blocked"
    if states and states != {"executable"}:
        return "contract_allowed_partial"
    return authored_boundary


def _authority_resolved_expected_gaps(
    authored_gaps: Mapping[str, Any],
    expected_states: Mapping[str, str],
) -> dict[str, str]:
    gap_states = {
        "source_unbound",
        "contract_partial",
        "permission_blocked",
        "snapshot_unavailable_as_of",
    }
    resolved: dict[str, str] = {}
    for dataset_id, state in expected_states.items():
        authored = str(authored_gaps.get(dataset_id) or "")
        if authored and _normalized_gap_state(authored) == state:
            resolved[dataset_id] = authored
        elif state in gap_states:
            resolved[dataset_id] = state
    return resolved


def _legacy_dataset_state_satisfies(*, expected: str, observed: str) -> bool:
    if expected == "degraded":
        return observed in {
            "executable",
            "degraded",
            "source_unbound",
            "contract_partial",
            "snapshot_unavailable_as_of",
        }
    return observed == expected


def _derive_capability_outcomes(
    required: tuple[str, ...],
    *,
    accepted_capabilities: set[str],
    authority: Mapping[str, Any],
    registry: RuntimeContractRegistry | None = None,
) -> dict[str, str]:
    plan_outcomes = (
        _derive_plan_capability_outcomes(authority, registry)
        if registry is not None
        else {}
    )

    blocked_capabilities: set[str] = set()
    terminal_gap_types = {
        "capability_metric_unsupported",
        "contract_absent",
        "contract_partial",
        "dataset_snapshot_unavailable_as_of",
        "permission_blocked",
        "source_unbound",
        "unsupported_grain",
        "window_data_unavailable",
    }
    for payload in _mapping_items_for_keys(authority, {"analysis_contract"}):
        try:
            contract = analysis_contract_from_dict(payload)
        except (KeyError, TypeError, ValueError):
            continue
        required_by_contract = set(contract.capability_requirements)
        for gap in contract.contract_gaps:
            if (
                gap.gap_type not in terminal_gap_types
                or not gap.owner
                or not gap.repair_options
                or not _gap_identity_matches_contract(gap, contract)
            ):
                continue
            blocked_capabilities.update(
                capability_id
                for capability_id in gap.affected_capabilities
                if capability_id in required_by_contract
            )

    outcomes: dict[str, str] = {}
    for capability_id in required:
        observed: set[str] = set(plan_outcomes.get(capability_id, ()))
        outcomes[capability_id] = (
            "executed"
            if "executed" in observed
            else "degraded"
            if "degraded" in observed
            else "blocked"
            if capability_id in blocked_capabilities
            else "missing_route"
            if capability_id not in accepted_capabilities
            else "unobserved"
        )
    return outcomes


def _derive_plan_capability_outcomes(
    authority: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> dict[str, set[str]]:
    admin = authority.get("admin_audit") or authority
    if not isinstance(admin, Mapping):
        return {}
    try:
        accepted_contract = analysis_contract_from_dict(admin.get("analysis_contract"))
    except (KeyError, TypeError, ValueError):
        return {}
    accepted_analysis_ref = accepted_contract.analysis_contract_id

    def records(key: str) -> tuple[Mapping[str, Any], ...]:
        value = admin.get(key) or ()
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    query_contracts: dict[str, Mapping[str, Any]] = {}
    query_objects: dict[str, QueryContract] = {}
    duplicate_query_refs: set[str] = set()
    for query in records("query_contracts"):
        query_ref = str(query.get("query_contract_id") or "")
        if (
            set(query) != set(QueryContract.__dataclass_fields__)
            or not query_ref
            or query_ref in query_contracts
        ):
            duplicate_query_refs.add(query_ref)
            continue
        try:
            signature = query_contract_signature(query)
        except (KeyError, TypeError, ValueError):
            continue
        if signature != str(query.get("contract_signature") or ""):
            continue
        try:
            query_object = _query_contract_from_mapping(query)
        except (KeyError, TypeError, ValueError):
            continue
        query_contracts[query_ref] = query
        query_objects[query_ref] = query_object
    for query_ref in duplicate_query_refs:
        query_contracts.pop(query_ref, None)
        query_objects.pop(query_ref, None)

    results_by_query: dict[str, list[Mapping[str, Any]]] = {}
    result_ref_counts: dict[str, int] = {}
    result_fields = set(QueryResultEnvelope.__dataclass_fields__) - {"rows"}
    for result in records("query_results"):
        if set(result) != result_fields:
            continue
        query_ref = str(result.get("query_contract_ref") or "")
        if query_ref:
            results_by_query.setdefault(query_ref, []).append(result)
        result_ref = str(result.get("result_ref") or "")
        if result_ref:
            result_ref_counts[result_ref] = result_ref_counts.get(result_ref, 0) + 1
    duplicate_result_refs = {
        result_ref for result_ref, count in result_ref_counts.items() if count > 1
    }
    if duplicate_result_refs:
        results_by_query = {
            query_ref: [
                result
                for result in results
                if str(result.get("result_ref") or "") not in duplicate_result_refs
            ]
            for query_ref, results in results_by_query.items()
        }
    reports_by_ref: dict[str, Mapping[str, Any]] = {}
    duplicate_report_refs: set[str] = set()
    for report in records("completeness_reports"):
        report_ref = str(report.get("report_ref") or "")
        if (
            set(report) != set(CompletenessReport.__dataclass_fields__)
            or not report_ref
            or report_ref in reports_by_ref
        ):
            duplicate_report_refs.add(report_ref)
            continue
        reports_by_ref[report_ref] = report
    for report_ref in duplicate_report_refs:
        reports_by_ref.pop(report_ref, None)

    outcomes: dict[str, set[str]] = {}
    for plan in records("capability_execution_plans"):
        plan_refs = {
            str(ref)
            for slot in (
                *(plan.get("required_input_slots") or ()),
                *(plan.get("optional_input_slots") or ()),
            )
            if isinstance(slot, Mapping)
            for ref in (
                *(slot.get("query_contract_refs") or ()),
                *(slot.get("validation_query_contract_refs") or ()),
            )
            if str(ref)
        }
        plan_queries = {
            ref: query_objects[ref] for ref in plan_refs if ref in query_objects
        }
        try:
            validate_capability_plan_semantics(plan, registry, plan_queries)
        except (AuthoritativeQueryChainError, KeyError, TypeError, ValueError):
            continue
        capability_id = str(plan.get("capability_id") or "")
        analysis_ref = str(plan.get("analysis_contract_ref") or "")
        if (
            analysis_ref != accepted_analysis_ref
            or capability_id not in accepted_contract.capability_requirements
        ):
            continue
        required_slots = tuple(
            slot
            for slot in plan.get("required_input_slots") or ()
            if isinstance(slot, Mapping) and slot.get("required") is True
        )
        if not required_slots:
            minimum_readiness = plan.get("minimum_readiness") or {}
            if (
                isinstance(minimum_readiness, Mapping)
                and minimum_readiness.get("required_slots") == "none"
                and not plan.get("required_input_slots")
                and not plan.get("optional_input_slots")
                and _queryless_completion_authority_passed(
                    capability_id,
                    authority=authority,
                    admin=admin,
                    registry=registry,
                )
            ):
                outcomes.setdefault(capability_id, set()).add("executed")
            continue
        slot_outcomes: list[str] = []
        valid = True
        for slot in required_slots:
            primary_refs = tuple(slot.get("query_contract_refs") or ())
            validation_refs = tuple(slot.get("validation_query_contract_refs") or ())
            if not primary_refs:
                valid = False
                break
            accepted = tuple(str(item) for item in slot.get("accepted_completeness") or ())
            for query_ref in (*primary_refs, *validation_refs):
                query = query_contracts.get(str(query_ref))
                is_validation = query_ref in validation_refs
                accepted_for_ref = ("complete",) if is_validation else accepted
                outcome = _persisted_query_outcome(
                    str(query_ref),
                    query=query,
                    analysis_contract_ref=analysis_ref,
                    accepted_completeness=accepted_for_ref,
                    results_by_query=results_by_query,
                    reports_by_ref=reports_by_ref,
                    expected_fields=(
                        () if is_validation else tuple(slot.get("required_fields") or ())
                    ),
                    expected_windows=(
                        ()
                        if is_validation
                        else tuple(slot.get("required_window_ids") or ())
                    ),
                )
                if not outcome:
                    valid = False
                    break
                slot_outcomes.append(outcome)
            if not valid:
                break
        if valid and slot_outcomes:
            outcomes.setdefault(capability_id, set()).add(
                "degraded" if "degraded" in slot_outcomes else "executed"
            )
    return outcomes


def _queryless_completion_authority_passed(
    capability_id: str,
    *,
    authority: Mapping[str, Any],
    admin: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> bool:
    accepted_contract = _run_matched_accepted_analysis_contract(authority)
    if (
        accepted_contract is None
        or capability_id not in accepted_contract.capability_requirements
    ):
        return False
    try:
        completion = str(
            registry.capability_inputs(capability_id).get(
                "completion_authority"
            )
            or ""
        )
    except KeyError:
        return False
    if completion == "verifier_passed":
        verifier = admin.get("verifier")
        return bool(
            isinstance(verifier, Mapping)
            and verifier.get("status") == "passed"
            and isinstance(verifier.get("errors"), (list, tuple))
            and not verifier.get("errors")
        )
    prefix = "checkpoint_completed:"
    if not completion.startswith(prefix):
        return False
    node = completion[len(prefix):]
    events = authority.get("checkpoint_events")
    if not isinstance(events, (list, tuple)):
        return False
    matching = tuple(
        event
        for event in events
        if isinstance(event, Mapping) and event.get("node") == node
    )
    return bool(matching and matching[-1].get("status") == "completed")


def _query_contract_from_mapping(value: Mapping[str, Any]) -> QueryContract:
    metrics = tuple(
        MetricBinding(
            **{
                **item,
                "required_fields": tuple(item.get("required_fields") or ()),
                "grain": tuple(item.get("grain") or ()),
                "claim_types": tuple(item.get("claim_types") or ()),
            }
        )
        for item in value.get("metric_bindings") or ()
        if isinstance(item, Mapping)
    )
    dimensions = tuple(
        DimensionBinding(
            **{
                **item,
                "allowed_grains": tuple(item.get("allowed_grains") or ()),
            }
        )
        for item in value.get("dimension_bindings") or ()
        if isinstance(item, Mapping)
    )
    windows = tuple(
        ResolvedWindow(**item)
        for item in value.get("resolved_windows") or ()
        if isinstance(item, Mapping)
    )
    shape = value.get("result_shape")
    if not isinstance(shape, Mapping):
        raise TypeError("query_result_shape_invalid")
    result_shape = ResultShape(
        **{
            **shape,
            "required_fields": tuple(shape.get("required_fields") or ()),
            "unique_key": tuple(shape.get("unique_key") or ()),
            "grain": tuple(shape.get("grain") or ()),
            "required_window_ids": tuple(shape.get("required_window_ids") or ()),
        }
    )
    reconciliation = value.get("reconciliation_binding")
    join = value.get("join_expectation")
    return QueryContract(
        query_contract_id=str(value["query_contract_id"]),
        analysis_contract_ref=str(value["analysis_contract_ref"]),
        query_intent=str(value["query_intent"]),
        dataset_snapshot_refs=tuple(value.get("dataset_snapshot_refs") or ()),
        metric_bindings=metrics,
        dimension_bindings=dimensions,
        window_refs=tuple(value.get("window_refs") or ()),
        resolved_windows=windows,
        filters=tuple(
            dict(item)
            for item in value.get("filters") or ()
            if isinstance(item, Mapping)
        ),
        result_shape=result_shape,
        completeness_assertions=tuple(value.get("completeness_assertions") or ()),
        permission_scope=str(value["permission_scope"]),
        workload_class=str(value["workload_class"]),
        contract_signature=str(value["contract_signature"]),
        query_parameters=dict(value.get("query_parameters") or {}),
        query_role_ref=str(value.get("query_role_ref") or ""),
        reconciliation_binding=(
            ReconciliationBinding(**reconciliation)
            if isinstance(reconciliation, Mapping)
            else None
        ),
        join_expectation=(
            JoinExpectation(
                **{
                    **join,
                    "audit_fields": tuple(join.get("audit_fields") or ()),
                }
            )
            if isinstance(join, Mapping)
            else None
        ),
    )


def _persisted_query_outcome(
    query_ref: str,
    *,
    query: Mapping[str, Any] | None,
    analysis_contract_ref: str,
    accepted_completeness: tuple[str, ...],
    results_by_query: Mapping[str, list[Mapping[str, Any]]],
    reports_by_ref: Mapping[str, Mapping[str, Any]],
    expected_fields: tuple[str, ...],
    expected_windows: tuple[str, ...],
) -> str:
    if (
        query is None
        or str(query.get("analysis_contract_ref") or "") != analysis_contract_ref
    ):
        return ""
    shape = query.get("result_shape") or {}
    if not isinstance(shape, Mapping):
        return ""
    if expected_fields and tuple(shape.get("required_fields") or ()) != expected_fields:
        return ""
    if expected_windows and tuple(shape.get("required_window_ids") or ()) != expected_windows:
        return ""
    results = results_by_query.get(query_ref) or ()
    if len(results) != 1:
        return ""
    result = results[0]
    if not _valid_persisted_result(result, query):
        return ""
    result_ref = str(result.get("result_ref") or "")
    report_ref = str(result.get("completeness_report_ref") or "")
    report = reports_by_ref.get(report_ref)
    if (
        str(result.get("execution_status") or "") != "succeeded"
        or not result_ref
        or not report_ref
        or report is None
        or str(report.get("query_contract_ref") or "") != query_ref
        or str(report.get("result_ref") or "") != result_ref
        or str(report.get("report_ref") or "") != report_ref
    ):
        return ""
    assertions = report.get("assertion_results") or ()
    if not _valid_persisted_report(report, result, query):
        return ""
    required_assertions = tuple(query.get("completeness_assertions") or ())
    observed_assertions = tuple(str(item.get("assertion") or "") for item in assertions)
    assertion_aliases = {
        "required_fields_present": "required_fields",
        "required_windows_complete": "required_windows",
        "source_snapshot_matches_contract": "snapshot_watermark",
        "unique_result_grain": "unique_key",
    }
    expected_assertions = tuple(
        assertion_aliases.get(name, name) for name in required_assertions
    )
    if any(observed_assertions.count(name) != 1 for name in expected_assertions):
        return ""
    completeness = str(report.get("completeness_status") or "")
    readiness = str(report.get("analysis_readiness") or "")
    if completeness == "complete" and readiness == "ready" and "complete" in accepted_completeness:
        return "executed"
    if completeness == "partial" and readiness == "degraded" and "partial" in accepted_completeness:
        return "degraded"
    return ""


def _strict_string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        return None
    if any(type(item) is not str or not item for item in value):
        return None
    result = tuple(value)
    return result if len(result) == len(set(result)) else None


def _valid_nested_value(value: Any) -> bool:
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return isfinite(value)
    if isinstance(value, Mapping):
        return all(
            type(key) is str and _valid_nested_value(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_valid_nested_value(child) for child in value)
    return False


def _valid_persisted_result(
    result: Mapping[str, Any], query: Mapping[str, Any]
) -> bool:
    string_fields = (
        "query_contract_ref",
        "query_id",
        "query_hash",
        "result_ref",
        "rows_ref",
        "completeness_report_ref",
        "execution_status",
        "execution_attempt_ref",
    )
    if any(type(result.get(field)) is not str or not result[field] for field in string_fields):
        return False
    if type(result.get("failure_reason")) is not str:
        return False
    if type(result.get("row_count")) is not int or result["row_count"] < 0:
        return False
    schema = result.get("observed_schema")
    provider_stats = result.get("provider_stats")
    if (
        not isinstance(schema, Mapping)
        or any(type(key) is not str or not key or type(value) is not str or not value for key, value in schema.items())
        or not isinstance(provider_stats, Mapping)
        or not _valid_nested_value(provider_stats)
    ):
        return False
    observed_windows = _strict_string_tuple(result.get("observed_windows"))
    observed_grain = _strict_string_tuple(result.get("observed_grain"))
    snapshot_refs = _strict_string_tuple(result.get("source_snapshot_refs"))
    if observed_windows is None or observed_grain is None or snapshot_refs is None:
        return False
    shape = query.get("result_shape")
    if not isinstance(shape, Mapping):
        return False
    required_fields = _strict_string_tuple(shape.get("required_fields"))
    expected_windows = _strict_string_tuple(shape.get("required_window_ids"))
    expected_grain = _strict_string_tuple(shape.get("grain"))
    contract_snapshots = _strict_string_tuple(query.get("dataset_snapshot_refs"))
    return bool(
        required_fields is not None
        and set(required_fields).issubset(schema)
        and expected_windows is not None
        and len(observed_windows) == len(expected_windows)
        and set(observed_windows) == set(expected_windows)
        and expected_grain is not None
        and observed_grain == expected_grain
        and contract_snapshots is not None
        and len(snapshot_refs) == len(contract_snapshots)
        and set(snapshot_refs) == set(contract_snapshots)
    )


def _valid_persisted_report(
    report: Mapping[str, Any],
    result: Mapping[str, Any],
    query: Mapping[str, Any],
) -> bool:
    for field in (
        "report_ref",
        "query_contract_ref",
        "result_ref",
        "completeness_status",
        "analysis_readiness",
    ):
        if type(report.get(field)) is not str or not report[field]:
            return False
    failures = _strict_string_tuple(report.get("failure_reasons"))
    assertions = report.get("assertion_results")
    coverage = report.get("coverage_summary")
    if failures is None or not isinstance(assertions, (list, tuple)) or not assertions:
        return False
    if not isinstance(coverage, Mapping) or not _valid_nested_value(coverage):
        return False
    known_assertions = {
        *CURRENT_DATA_ASSERTIONS,
        "dimension_total_reconciliation",
        "join_cardinality",
        "paired_target_baseline",
    }
    identities: list[str] = []
    assertion_failure_reasons: list[str] = []
    for assertion in assertions:
        if not isinstance(assertion, Mapping) or set(assertion) != {
            "assertion", "passed", "failure_reasons", "details"
        }:
            return False
        identity = assertion.get("assertion")
        assertion_failures = _strict_string_tuple(assertion.get("failure_reasons"))
        if (
            type(identity) is not str
            or identity not in known_assertions
            or type(assertion.get("passed")) is not bool
            or assertion_failures is None
            or not isinstance(assertion.get("details"), Mapping)
            or not _valid_nested_value(assertion["details"])
            or assertion["passed"] != (not assertion_failures)
        ):
            return False
        identities.append(identity)
        assertion_failure_reasons.extend(assertion_failures)
    if len(identities) != len(set(identities)):
        return False
    deduped_assertion_failures = tuple(dict.fromkeys(assertion_failure_reasons))
    if failures != deduped_assertion_failures:
        return False
    completeness = report["completeness_status"]
    readiness = report["analysis_readiness"]
    if completeness == "complete" and readiness == "ready":
        if failures or any(assertion["passed"] is not True for assertion in assertions):
            return False
    elif completeness == "partial" and readiness in {"degraded", "blocked"}:
        if not failures or all(assertion["passed"] is True for assertion in assertions):
            return False
    else:
        return False
    expected_coverage = {
        "row_count": result.get("row_count"),
        "required_windows": list(query.get("window_refs") or ()),
        "observed_windows": list(result.get("observed_windows") or ()),
        "expected_grain": list((query.get("result_shape") or {}).get("grain") or ()),
        "observed_grain": list(result.get("observed_grain") or ()),
        "snapshot_refs": list(result.get("source_snapshot_refs") or ()),
        "rows_ref": result.get("rows_ref"),
    }
    for field, expected in expected_coverage.items():
        actual = coverage.get(field)
        if isinstance(expected, list):
            typed_actual = _strict_string_tuple(actual)
            if typed_actual is None or (
                (field in {"required_windows", "observed_windows", "snapshot_refs"})
                and (len(typed_actual) != len(expected) or set(typed_actual) != set(expected))
            ) or (
                field not in {"required_windows", "observed_windows", "snapshot_refs"}
                and typed_actual != tuple(expected)
            ):
                return False
        elif type(actual) is not type(expected) or actual != expected:
            return False
    snapshots = tuple(result.get("source_snapshot_refs") or ())
    expected_snapshot_ref = snapshots[0] if len(snapshots) == 1 else ""
    return coverage.get("snapshot_ref") == expected_snapshot_ref


def _gap_identity_matches_contract(gap: Any, contract: Any) -> bool:
    parts = gap.gap_id.split(":")
    if len(parts) < 2 or any(not part for part in parts[:2]):
        return False
    namespace, object_id = parts[:2]
    datasets = set(contract.dataset_requirements)
    capabilities = set(contract.capability_requirements)
    requested_metrics = {
        str(item) for item in contract.scope.get("requested_metric_ids") or ()
    }
    requested_dimensions = {
        str(item) for item in contract.scope.get("requested_dimension_ids") or ()
    }
    bound_object = {
        "dataset": object_id in datasets and gap.dataset_id == object_id,
        "capability": object_id in capabilities,
        "metric": object_id in requested_metrics
        or object_id in {binding.metric_id for binding in contract.metric_bindings},
        "dimension": object_id in requested_dimensions
        or object_id in {
            binding.dimension_id for binding in contract.dimension_bindings
        },
        "claim_intent": object_id in set(gap.affected_claim_types),
        "claim_intents": object_id == "unbound",
        "window": True,
    }.get(namespace, False)
    if gap.gap_type == "window_data_unavailable":
        return (
            gap.dataset_id in datasets
            and len(parts) == 5
            and parts[0] == gap.dataset_id
            and parts[1] == "target_day"
            and bool(parts[2])
            and parts[3] == "watermark"
            and bool(parts[4])
        )
    if not bound_object:
        return False
    if gap.gap_type == "contract_absent":
        if (
            namespace in {"metric", "dimension"}
            and len(parts) == 4
            and parts[2] == "source_unavailable"
        ):
            return bool(parts[3])
        if namespace == "capability" and len(parts) == 5:
            return (
                parts[2] == "query_shape"
                and bool(parts[3])
                and parts[4] == "contract_absent"
            )
        return (
            namespace in {"metric", "dimension", "dataset", "capability"}
            and len(parts) == 3
            and parts[2] == "contract_absent"
        )
    if gap.gap_type in {
        "dataset_snapshot_unavailable_as_of",
        "permission_blocked",
        "source_unbound",
    }:
        return (
            namespace == "dataset"
            and len(parts) == 3
            and parts[2] == gap.gap_type
        )
    if gap.gap_type == "unsupported_grain":
        return (
            namespace == "dimension"
            and len(parts) == 4
            and parts[2] == "grain"
            and bool(parts[3])
        )
    if gap.gap_type == "capability_metric_unsupported":
        return (
            namespace == "metric"
            and len(parts) == 3
            and parts[2] == "capability_metric_family_unsupported"
        )
    if gap.gap_type == "contract_partial":
        return _contract_partial_gap_id_valid(parts, capabilities)
    return True


def _contract_partial_gap_id_valid(
    parts: list[str], capabilities: set[str]
) -> bool:
    namespace, object_id = parts[:2]
    if namespace == "claim_intents":
        return parts == ["claim_intents", "unbound"]
    if namespace == "window":
        return len(parts) == 3 and object_id in {
            "duplicate_baseline",
            "unsupported_baseline",
            "unsupported_target_semantic",
        }
    if len(parts) < 3:
        return False
    marker = parts[2]
    if namespace == "dataset":
        if marker in {"contract_partial", "schema_missing"}:
            return len(parts) == 4 and bool(parts[3])
        return (
            marker == "evidence_state"
            and len(parts) == 6
            and bool(parts[3])
            and parts[4] == "capability"
            and parts[5] in capabilities
        )
    if namespace == "metric":
        if marker in {"missing", "schema_missing", "source_ambiguous"}:
            return len(parts) == 4 and bool(parts[3])
        if marker == "invalid":
            return len(parts) == 4 and parts[3] in {
                "display_policy",
                "reconciliation_strategy",
                "reconciliation_tolerance",
            }
        return marker == "capability_metric_family_unsupported" and len(parts) == 3
    if namespace == "dimension":
        return (
            marker in {"missing", "schema_missing", "source_ambiguous"}
            and len(parts) == 4
            and bool(parts[3])
        )
    if namespace == "claim_intent":
        return marker == "unsupported" and len(parts) == 3
    if namespace != "capability":
        return False
    if marker == "missing":
        return len(parts) == 4 and bool(parts[3])
    if marker == "query_shape":
        return (
            len(parts) == 6
            and bool(parts[3])
            and parts[4] == "missing"
            and bool(parts[5])
        )
    if marker in {"required_window", "required_query"}:
        return len(parts) == 5 and bool(parts[3]) and parts[4] == "unbound"
    if marker in {"required_context_source", "required_dimension"}:
        return len(parts) == 4 and parts[3] == "unbound"
    return False


_DATASET_STATE_PRECEDENCE = {
    "executable": 0,
    "degraded": 1,
    "source_unbound": 2,
    "contract_partial": 3,
    "snapshot_unavailable_as_of": 4,
    "permission_blocked": 5,
}


def _derive_runtime_dataset_states(
    authority: Mapping[str, Any],
    *,
    registry: RuntimeContractRegistry | None = None,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    states: dict[str, str] = {}
    gaps: dict[str, list[str]] = {}
    for item in _mapping_items_for_keys(
        authority,
        {"query_executions", "query_results", "capability_bindings"},
    ):
        dataset_ids = _dataset_ids(item)
        execution = str(item.get("execution_status") or item.get("status") or "")
        completeness_record = item.get("completeness")
        completeness = str(item.get("completeness_status") or "")
        if not completeness and isinstance(completeness_record, Mapping):
            completeness = str(
                completeness_record.get("completeness_status") or ""
            )
        if execution not in {
            "succeeded",
            "completed",
            "executed",
            "ready",
            "degraded",
        }:
            continue
        state = (
            "executable"
            if completeness in {"complete", "ready"} or execution == "ready"
            else "degraded"
        )
        for dataset_id in dataset_ids:
            _set_dataset_state(states, dataset_id, state)
    accepted_contract = _run_matched_accepted_analysis_contract(authority)
    contract_gaps = accepted_contract.contract_gaps if accepted_contract else ()
    for contract_gap in contract_gaps:
        item = contract_gap.to_dict()
        gap_type = str(item.get("gap_type") or item.get("error_code") or "")
        normalized = _persisted_dataset_gap_state(item, registry=registry)
        if not normalized:
            continue
        for dataset_id in _validated_gap_dataset_ids(item):
            gaps.setdefault(dataset_id, []).append(gap_type)
            _set_dataset_state(states, dataset_id, normalized)
    return states, {
        dataset_id: tuple(dict.fromkeys(items)) for dataset_id, items in gaps.items()
    }


def _run_matched_accepted_analysis_contract(
    authority: Mapping[str, Any],
):
    run_id = authority.get("run_id")
    admin = authority.get("admin_audit")
    if not isinstance(run_id, str) or not run_id or not isinstance(admin, Mapping):
        return None
    persisted = admin.get("analysis_contract")
    runtime_plan = admin.get("compiler_runtime_plan")
    if not isinstance(persisted, Mapping) or not isinstance(runtime_plan, Mapping):
        return None
    planned = runtime_plan.get("analysis_contract")
    if not isinstance(planned, Mapping):
        return None
    try:
        persisted_contract = analysis_contract_from_dict(persisted)
        planned_contract = analysis_contract_from_dict(planned)
        persisted_signature = analysis_contract_signature(persisted_contract)
        planned_signature = analysis_contract_signature(planned_contract)
    except (KeyError, TypeError, ValueError):
        return None
    expected_ref = f"analysis:{run_id}:1"
    if (
        persisted_contract.analysis_contract_id != expected_ref
        or planned_contract.analysis_contract_id != expected_ref
        or persisted_signature != planned_signature
    ):
        return None
    return persisted_contract


def _persisted_dataset_gap_state(
    gap: Mapping[str, Any],
    *,
    registry: RuntimeContractRegistry | None,
) -> str:
    gap_type = str(gap.get("gap_type") or gap.get("error_code") or "")
    if gap_type != "contract_partial" or registry is None:
        return _normalized_gap_state(gap_type)
    dataset_id = str(gap.get("dataset_id") or "")
    parts = str(gap.get("gap_id") or "").split(":")
    if (
        len(parts) != 6
        or parts[:1] != ["dataset"]
        or parts[1] != dataset_id
        or parts[2:5] != ["evidence_state", "context_only", "capability"]
        or not parts[5]
        or tuple(gap.get("affected_capabilities") or ()) != (parts[5],)
        or str(gap.get("owner") or "") != "data_owner"
        or len(tuple(gap.get("repair_options") or ())) != 3
        or set(gap.get("repair_options") or ())
        != {
            "use_context_only_query",
            "publish_claim_ready_release",
            "resolve_reconciliation",
        }
    ):
        return "contract_partial"
    try:
        policy = registry.capability_inputs(parts[5]).get("degradation_policy") or {}
    except KeyError:
        return "contract_partial"
    return (
        "degraded"
        if isinstance(policy, Mapping)
        and policy.get("incomplete_input") == "context_only"
        else "contract_partial"
    )


def _mapping_items_for_keys(
    value: Any,
    keys: set[str],
) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in keys:
                candidates = child if isinstance(child, (list, tuple)) else (child,)
                output.extend(item for item in candidates if isinstance(item, Mapping))
            output.extend(_mapping_items_for_keys(child, keys))
    elif isinstance(value, (list, tuple)):
        for child in value:
            output.extend(_mapping_items_for_keys(child, keys))
    return output


def _dataset_ids(item: Mapping[str, Any]) -> tuple[str, ...]:
    output: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            single = str(value.get("dataset_id") or "")
            if single:
                output.append(single)
            for key in ("dataset_ids", "required_datasets"):
                values = value.get(key) or ()
                if isinstance(values, str):
                    values = (values,)
                output.extend(str(candidate) for candidate in values if candidate)
            gap_id = str(value.get("gap_id") or "")
            if gap_id.startswith("dataset:"):
                parts = gap_id.split(":", 2)
                if len(parts) == 3 and parts[1]:
                    output.append(parts[1])
            error_code = str(value.get("error_code") or "")
            if error_code.startswith("missing_required_dataset:"):
                dataset_id = error_code.split(":", 1)[1]
                if dataset_id:
                    output.append(dataset_id)
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(item)
    return tuple(dict.fromkeys(output))


def _validated_gap_dataset_ids(gap: Mapping[str, Any]) -> tuple[str, ...]:
    dataset_id = str(gap.get("dataset_id") or "")
    gap_id = str(gap.get("gap_id") or "")
    if gap_id.startswith("dataset:"):
        parts = gap_id.split(":", 2)
        if len(parts) != 3 or not dataset_id or parts[1] != dataset_id:
            return ()
        return (dataset_id,)
    if dataset_id:
        return (dataset_id,)
    return tuple(
        dataset
        for dataset in _dataset_ids(gap)
        if dataset
    )


def _set_dataset_state(states: dict[str, str], dataset_id: str, state: str) -> None:
    current = states.get(dataset_id)
    if current is None or _DATASET_STATE_PRECEDENCE[state] >= _DATASET_STATE_PRECEDENCE[current]:
        states[dataset_id] = state


def _normalized_gap_state(gap_type: str) -> str:
    aliases = {
        "permission_blocked": "permission_blocked",
        "contract_partial": "contract_partial",
        "contract_absent": "source_unbound",
        "missing_contract": "source_unbound",
        "source_unbound": "source_unbound",
        "dataset_snapshot_unavailable_as_of": "snapshot_unavailable_as_of",
        "snapshot_unavailable_as_of": "snapshot_unavailable_as_of",
    }
    if gap_type.startswith("missing_required_dataset:"):
        return "source_unbound"
    return aliases.get(gap_type, "")


def _gap_expectation_matches(expected: str, actual: tuple[str, ...]) -> bool:
    return expected in actual or any(
        _normalized_gap_state(item) == _normalized_gap_state(expected)
        for item in actual
        if _normalized_gap_state(item)
    )


def _review_claim_ceiling(
    authority: Mapping[str, Any],
    scenario: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    *,
    evidence_resolver: Any = None,
    rows_loader: Any = None,
    release_resolver: Any = None,
) -> dict[str, Any]:
    claims, conflicting_claim_refs = _deduplicated_verified_claims(authority)
    bindings = _mapping_items_for_keys(authority, {"capability_bindings"})
    evidence = _mapping_items_for_keys(authority, {"evidence_manifests", "evidence"})
    provenance = _mapping_items_for_keys(
        authority,
        {
            "trusted_claim_provenance_records",
            "trusted_provenance_records",
            "claim_provenance_records",
        },
    )
    binding_by_ref = {
        ref: item
        for item in bindings
        for ref in (
            str(item.get("binding_manifest_ref") or ""),
            str(item.get("record_ref") or ""),
            str(item.get("binding_ref") or ""),
        )
        if ref
    }
    evidence_by_ref = {
        str(item.get("evidence_ref") or ""): item
        for item in evidence
        if str(item.get("evidence_ref") or "")
    }
    provenance_by_ref = {
        str(item.get("provenance_record_ref") or item.get("record_ref") or ""): item
        for item in provenance
        if str(item.get("provenance_record_ref") or item.get("record_ref") or "")
    }
    allowed = str(scenario.get("allowed_claim_ceiling") or "")
    allowed_rank = registry.maximum_claim_strength_rank(allowed)
    claim_reviews: list[dict[str, Any]] = []
    missing: list[str] = []
    producing_ceilings: list[str] = []
    strengths: list[str] = []
    for index, claim in enumerate(claims):
        claim_ref = str(claim.get("claim_ref") or f"claim-index:{index}")
        strength = str(
            claim.get("claim_strength") or claim.get("strength") or "insufficient"
        )
        strengths.append(strength)
        if claim_ref in conflicting_claim_refs:
            claim_reviews.append(
                {
                    "claim_ref": claim_ref,
                    "claim_strength": strength,
                    "producing_capabilities": [],
                    "authority_ceiling": "",
                    "passed": False,
                    "error_code": "conflicting_claim_ref_payload",
                    "authority_errors": ["conflicting_claim_ref_payload"],
                }
            )
            continue
        support_evidence = {
            str(ref) for ref in claim.get("evidence_refs") or () if ref
        }
        claim_producing_results = {
            str(ref) for ref in claim.get("result_refs") or () if ref
        }
        provenance_record = provenance_by_ref.get(
            str(claim.get("provenance_record_ref") or "")
        )
        if provenance_record:
            support_evidence.update(
                str(ref) for ref in provenance_record.get("evidence_refs") or () if ref
            )
            claim_producing_results.update(
                str(ref) for ref in provenance_record.get("result_refs") or () if ref
            )
        support_results = set(claim_producing_results)
        related: dict[str, Mapping[str, Any]] = {}
        authority_errors: list[str] = []
        authorized_claim_results: set[str] = set()
        if evidence_resolver is not None and not claim_producing_results:
            authority_errors.append("claim_result_refs_missing")
        for evidence_ref in support_evidence:
            manifest = evidence_by_ref.get(evidence_ref)
            if not manifest:
                if evidence_resolver is not None:
                    authority_errors.append("claim_evidence_manifest_missing")
                continue
            binding_ref = str(manifest.get("binding_manifest_ref") or "")
            support_results.update(
                str(ref) for ref in manifest.get("result_refs") or () if ref
            )
            if evidence_resolver is not None:
                binding, authorized_results, errors = _resolve_claim_capability_binding(
                    manifest,
                    claim_type=str(claim.get("claim_type") or ""),
                    claim_producing_result_refs=claim_producing_results,
                    evidence_resolver=evidence_resolver,
                    rows_loader=rows_loader,
                    release_resolver=release_resolver,
                    registry=registry,
                )
                authorized_claim_results.update(authorized_results)
                authority_errors.extend(errors)
                if binding is not None and not errors:
                    related[str(binding.record_ref)] = binding
            elif binding_ref in binding_by_ref:
                related[binding_ref] = binding_by_ref[binding_ref]
        if evidence_resolver is None:
            for binding_ref, binding in binding_by_ref.items():
                binding_results = {
                    str(ref) for ref in binding.get("result_refs") or () if ref
                }
                if support_results & binding_results:
                    related[binding_ref] = binding
        elif (
            not authority_errors
            and claim_producing_results
            and not claim_producing_results.issubset(authorized_claim_results)
        ):
            authority_errors.append("claim_result_refs_not_bound")
        authority_errors = list(dict.fromkeys(authority_errors))
        if authority_errors:
            missing.append(claim_ref)
            claim_reviews.append(
                {
                    "claim_ref": claim_ref,
                    "claim_strength": strength,
                    "producing_capabilities": [],
                    "authority_ceiling": "",
                    "passed": False,
                    "error_code": "claim_capability_authority_invalid",
                    "authority_errors": authority_errors,
                }
            )
            continue
        ceilings = tuple(
            str(
                binding.maximum_claim_strength
                if evidence_resolver is not None
                else binding.get("maximum_claim_strength") or ""
            )
            for binding in related.values()
            if str(
                binding.maximum_claim_strength
                if evidence_resolver is not None
                else binding.get("maximum_claim_strength") or ""
            )
        )
        if not ceilings:
            missing.append(claim_ref)
            claim_reviews.append(
                {
                    "claim_ref": claim_ref,
                    "claim_strength": strength,
                    "producing_capabilities": [],
                    "authority_ceiling": "",
                    "passed": False,
                    "error_code": "missing_claim_capability_provenance",
                    "authority_errors": [],
                }
            )
            continue
        ceiling = min(ceilings, key=registry.maximum_claim_strength_rank)
        producing_ceilings.append(ceiling)
        passed = (
            registry.claim_strength_rank(strength)
            <= registry.maximum_claim_strength_rank(ceiling)
            and registry.claim_strength_rank(strength) <= allowed_rank
        )
        claim_reviews.append(
            {
                "claim_ref": claim_ref,
                "claim_strength": strength,
                "producing_capabilities": sorted(
                    {
                        str(
                            binding.capability_id
                            if evidence_resolver is not None
                            else binding.get("capability_id") or ""
                        )
                        for binding in related.values()
                        if (
                            binding.capability_id
                            if evidence_resolver is not None
                            else binding.get("capability_id")
                        )
                    }
                ),
                "authority_ceiling": ceiling,
                "passed": passed,
                "error_code": "" if passed else "claim_strength_exceeds_authority",
                "authority_errors": [],
            }
        )
    actual_strength = (
        max(strengths, key=registry.claim_strength_rank) if strengths else "insufficient"
    )
    actual_ceiling = (
        min(producing_ceilings, key=registry.maximum_claim_strength_rank)
        if producing_ceilings
        else ""
    )
    return {
        "actual_max_claim_strength": actual_strength,
        "actual_authority_ceiling": actual_ceiling,
        "claim_authority_reviews": claim_reviews,
        "missing_claim_capability_provenance": missing,
        "passed": not missing and all(item["passed"] for item in claim_reviews),
    }


def _deduplicated_verified_claims(
    authority: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], set[str]]:
    claims: list[Mapping[str, Any]] = []
    canonical_by_ref: dict[str, Any] = {}
    conflicting_refs: set[str] = set()
    for claim in _mapping_items_for_keys(authority, {"verified_claims"}):
        claim_ref = str(claim.get("claim_ref") or "")
        if not claim_ref:
            claims.append(claim)
            continue
        try:
            payload = canonical_value(claim)
        except (EvidenceIntegrityError, TypeError, ValueError):
            payload = None
            conflicting_refs.add(claim_ref)
        if claim_ref not in canonical_by_ref:
            canonical_by_ref[claim_ref] = payload
            claims.append(claim)
        elif canonical_by_ref[claim_ref] != payload:
            conflicting_refs.add(claim_ref)
    return claims, conflicting_refs


def _resolve_claim_capability_binding(
    evidence: Mapping[str, Any],
    *,
    claim_type: str,
    claim_producing_result_refs: set[str],
    evidence_resolver: Any,
    rows_loader: Any,
    release_resolver: Any,
    registry: RuntimeContractRegistry,
) -> tuple[Any, frozenset[str], tuple[str, ...]]:
    binding_ref = str(evidence.get("binding_manifest_ref") or "")
    if not binding_ref:
        return None, frozenset(), ("capability_binding_record_ref_missing",)
    if not isinstance(evidence_resolver, RuntimeEvidenceResolver):
        return None, frozenset(), ("runtime_evidence_resolver_invalid",)
    if not isinstance(rows_loader, RowsPayloadLoader):
        return None, frozenset(), ("rows_payload_loader_invalid",)
    try:
        binding = evidence_resolver.resolve_capability_binding(binding_ref)
    except Exception:
        return None, frozenset(), ("capability_binding_resolution_failed",)
    if binding is None:
        return None, frozenset(), ("capability_binding_record_missing",)
    if type(binding) is not CapabilityBindingRecord:
        return None, frozenset(), ("capability_binding_record_type_invalid",)
    if runtime_evidence_record_integrity_errors(binding):
        return None, frozenset(), ("capability_binding_record_integrity",)

    errors: list[str] = []
    if str(binding.record_ref) != binding_ref:
        errors.append("capability_binding_record_ref_mismatch")
    if str(binding.binding_digest) != str(
        evidence.get("binding_manifest_digest") or ""
    ):
        errors.append("binding_manifest_digest_mismatch")
    binding_result_closure = {
        str(ref)
        for ref in (*binding.result_refs, *binding.validation_result_refs)
        if ref
    }
    observed_results = {
        str(ref) for ref in evidence.get("result_refs") or () if ref
    }
    if not observed_results or not observed_results.issubset(
        binding_result_closure
    ):
        errors.append("capability_binding_result_refs_mismatch")
    primary_results = {
        str(ref) for ref in binding.result_refs if str(ref)
    }
    manifest_claim_results = claim_producing_result_refs.intersection(
        observed_results
    )
    authorized_claim_results: frozenset[str] = frozenset()
    if claim_producing_result_refs and not manifest_claim_results:
        errors.append("claim_evidence_result_refs_missing")
    elif manifest_claim_results and not manifest_claim_results.issubset(
        primary_results
    ):
        errors.append("claim_result_refs_not_primary")
    else:
        authorized_claim_results = frozenset(manifest_claim_results)
    if binding.status != "ready":
        errors.append("capability_binding_not_ready")
    if (
        not binding.input_completeness_statuses
        or any(
            status != "complete"
            for status in binding.input_completeness_statuses
        )
    ):
        errors.append("capability_binding_input_completeness_not_complete")

    capability_id = str(binding.capability_id or "")
    try:
        policy = registry.capability_inputs(capability_id)
        expected_signature = registry.capability_contract_signature(capability_id)
        expected_ref = registry.capability_contract_ref(capability_id)
        expected_ceiling = str(policy.get("maximum_claim_strength") or "")
        expected_ceiling_rank = registry.maximum_claim_strength_rank(
            expected_ceiling
        )
        expected_claim_types = tuple(policy.get("supported_claim_types") or ())
        expected_evidence_types = tuple(
            policy.get("supported_evidence_types") or ()
        )
    except (KeyError, TypeError, ValueError):
        errors.append("capability_contract_registry_missing")
    else:
        if binding.capability_contract_signature != expected_signature:
            errors.append("capability_contract_signature_mismatch")
        if binding.capability_contract_version != registry.contract_version:
            errors.append("capability_contract_version_mismatch")
        if str(binding.plan_payload.get("capability_contract_ref") or "") != expected_ref:
            errors.append("capability_contract_ref_mismatch")
        if (
            binding.maximum_claim_strength != expected_ceiling
            or str(binding.plan_payload.get("maximum_claim_strength") or "")
            != expected_ceiling
            or binding.maximum_claim_strength_rank != expected_ceiling_rank
            or binding.plan_payload.get("maximum_claim_strength_rank")
            != expected_ceiling_rank
        ):
            errors.append("capability_claim_ceiling_mismatch")
        if (
            binding.claim_strength_taxonomy_version
            != registry.claim_strength_taxonomy_version
            or binding.plan_payload.get("claim_strength_taxonomy_version")
            != registry.claim_strength_taxonomy_version
        ):
            errors.append("claim_strength_taxonomy_version_mismatch")
        if tuple(binding.supported_claim_types) != expected_claim_types:
            errors.append("capability_supported_claim_types_mismatch")
        if tuple(binding.supported_evidence_types) != expected_evidence_types:
            errors.append("capability_supported_evidence_types_mismatch")
        if not claim_type or (
            claim_type not in binding.supported_claim_types
            or claim_type not in expected_claim_types
        ):
            errors.append(
                "claim_type_missing" if not claim_type else "claim_type_not_supported"
            )
        evidence_type = str(evidence.get("evidence_type") or "")
        if not evidence_type or (
            evidence_type not in binding.supported_evidence_types
            or evidence_type not in expected_evidence_types
        ):
            errors.append(
                "evidence_type_missing"
                if not evidence_type
                else "evidence_type_not_supported"
            )

    try:
        chain = validate_authoritative_query_chain(
            binding,
            resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=registry,
            release_resolver=release_resolver,
        )
    except AuthoritativeQueryChainError as exc:
        errors.append(f"authoritative_query_chain_invalid:{exc}")
    except Exception:
        errors.append("authoritative_query_chain_resolution_failed")
    else:
        reports = (*chain.primary_reports, *chain.validation_reports)
        if (
            not reports
            or any(
                report.completeness_status != "complete"
                or report.analysis_readiness != "ready"
                for report in reports
            )
        ):
            errors.append("authoritative_query_chain_not_claim_ready")
    return (
        binding,
        authorized_claim_results,
        tuple(dict.fromkeys(errors)),
    )


def _review_terminal_boundary(
    turn_record: Mapping[str, Any],
    scenario: Mapping[str, Any],
    observed_states: Mapping[str, str],
) -> dict[str, Any]:
    boundary = str(scenario.get("terminal_boundary") or "")
    status = str(turn_record.get("resumed_status") or turn_record.get("status") or "")
    answer_package = (
        turn_record.get("resumed_answer_package")
        if turn_record.get("resumed_status")
        else turn_record.get("answer_package")
    )
    has_answer = bool(
        answer_package or (turn_record.get("runtime_authority") or {}).get("verified_claims")
    )
    matches = {
        "verified_answer": status == "completed" and has_answer,
        "permission_blocked": (
            status == "completed" and "permission_blocked" in observed_states.values()
        ),
        "contract_allowed_partial": status == "completed" and any(
            state in {
                "contract_partial",
                "degraded",
                "source_unbound",
                "snapshot_unavailable_as_of",
            }
            for state in observed_states.values()
        ),
    }
    passed = matches.get(boundary, False)
    return {
        "outcome": boundary if passed else f"unmet:{boundary}:{status or 'missing'}",
        "passed": passed,
    }


def _review_required_reuse(
    authority: Mapping[str, Any],
    expected: Any,
    *,
    registry: RuntimeContractRegistry,
    evidence_resolver: Any,
    rows_loader: Any,
    release_resolver: Any,
    conversation_store: Any = None,
    case_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    empty_review = {
        "passed": False,
        "errors": [],
        "source_result_ref": "",
        "current_result_ref": "",
        "query_contract_ref": "",
        "capability_id": "",
        "dataset_ids": [],
    }
    expected_tuple = _expected_reuse_tuple(expected)
    if expected_tuple is None:
        return {**empty_review, "errors": ["expected_reuse_schema_invalid"]}
    expected_capability, expected_datasets = expected_tuple
    admin = authority.get("admin_audit")
    if not isinstance(admin, Mapping):
        return {**empty_review, "errors": ["run_matched_reuse_authority_missing"]}
    raw_decisions = admin.get("reuse_decisions")
    if not isinstance(raw_decisions, (list, tuple)):
        return {**empty_review, "errors": ["admin_reuse_decision_missing"]}
    decisions = tuple(
        item
        for item in raw_decisions
        if isinstance(item, Mapping) and item.get("decision") == "reuse"
    )
    if not decisions:
        return {**empty_review, "errors": ["admin_reuse_decision_missing"]}

    candidates: list[dict[str, Any]] = []
    for decision in decisions:
        candidate_authority = {
            **authority,
            "admin_audit": {**admin, "reuse_decisions": [decision]},
        }
        candidate = _review_required_reuse_candidate(
            candidate_authority,
            {
                "capability_id": expected_capability,
                "dataset_ids": sorted(expected_datasets),
            },
            registry=registry,
            evidence_resolver=evidence_resolver,
            rows_loader=rows_loader,
            release_resolver=release_resolver,
            conversation_store=conversation_store,
            case_lineage=case_lineage,
        )
        if candidate.get("capability_id") == expected_capability:
            candidates.append(candidate)
    if len(candidates) > 1:
        return {**empty_review, "errors": ["expected_reuse_tuple_ambiguous"]}
    if not candidates:
        return {**empty_review, "errors": ["reuse_current_result_mismatch"]}
    return candidates[0]


def _expected_reuse_tuple(value: Any) -> tuple[str, frozenset[str]] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "capability_id",
        "dataset_ids",
    }:
        return None
    capability_id = value.get("capability_id")
    dataset_ids = value.get("dataset_ids")
    if (
        not isinstance(capability_id, str)
        or not capability_id.strip()
        or capability_id != capability_id.strip()
        or not isinstance(dataset_ids, list)
        or not dataset_ids
        or any(
            not isinstance(dataset_id, str)
            or not dataset_id.strip()
            or dataset_id != dataset_id.strip()
            for dataset_id in dataset_ids
        )
        or len(set(dataset_ids)) != len(dataset_ids)
    ):
        return None
    return capability_id, frozenset(dataset_ids)


def _normalized_reuse_case_lineage(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    thread_id = value.get("thread_id")
    current_run_id = value.get("current_run_id")
    current_topic_id = value.get("current_topic_id")
    raw_prior = value.get("prior_runs")
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(current_run_id, str)
        or not current_run_id
        or not isinstance(current_topic_id, str)
        or not current_topic_id
        or not isinstance(raw_prior, (list, tuple))
    ):
        return None
    prior_runs: list[dict[str, str]] = []
    for item in raw_prior:
        if not isinstance(item, Mapping):
            return None
        normalized = {
            key: item.get(key)
            for key in ("run_id", "thread_id", "topic_id", "status")
        }
        if any(not isinstance(field, str) or not field for field in normalized.values()):
            return None
        prior_runs.append({key: str(field) for key, field in normalized.items()})
    if len({item["run_id"] for item in prior_runs}) != len(prior_runs):
        return None
    return {
        "thread_id": thread_id,
        "current_run_id": current_run_id,
        "current_topic_id": current_topic_id,
        "prior_runs": prior_runs,
    }


def _review_published_source_candidate(
    *,
    source_ref: str,
    expected_capability: str,
    current_analysis_contract_ref: str,
    registry: RuntimeContractRegistry,
    evidence_resolver: RuntimeEvidenceResolver,
    rows_loader: RowsPayloadLoader,
    release_resolver: Any,
    conversation_store: Any,
    case_lineage: Mapping[str, Any] | None,
    decision_candidate_signature: str,
    current_candidate_signature: str,
) -> tuple[QueryExecutionRecord | None, list[str]]:
    errors: list[str] = []
    lineage = _normalized_reuse_case_lineage(case_lineage)
    if lineage is None:
        return None, ["reuse_case_lineage_invalid"]
    resolver_method = getattr(
        conversation_store,
        "resolve_result_candidate_authority",
        None,
    )
    if not callable(resolver_method):
        return None, ["reuse_source_candidate_authority_invalid"]
    try:
        source_authority = resolver_method(
            result_ref=source_ref,
            topic_id=lineage["current_topic_id"],
        )
    except Exception:
        return None, ["reuse_source_candidate_authority_invalid"]
    if not isinstance(source_authority, Mapping):
        return None, ["reuse_source_candidate_authority_invalid"]
    result_ref_record = source_authority.get("result_ref_record")
    if not isinstance(result_ref_record, Mapping):
        return None, ["reuse_source_candidate_authority_invalid"]
    try:
        candidate = validate_result_reuse_candidate(
            result_ref_record.get("payload") or {}
        )
    except (EvidenceIntegrityError, TypeError, ValueError):
        return None, ["reuse_source_candidate_authority_invalid"]
    signed_candidate_signature = str(candidate.get("candidate_signature") or "")
    if (
        not signed_candidate_signature
        or decision_candidate_signature != signed_candidate_signature
        or current_candidate_signature != signed_candidate_signature
    ):
        errors.append("reuse_candidate_signature_lineage_mismatch")

    source_run_id = str(source_authority.get("source_run_id") or "")
    if source_run_id == lineage["current_run_id"]:
        errors.append("reuse_source_run_not_prior")
    prior_matches = tuple(
        item
        for item in lineage["prior_runs"]
        if item["run_id"] == source_run_id
        and item["thread_id"] == lineage["thread_id"]
        and item["topic_id"] == lineage["current_topic_id"]
        and item["status"] == "completed"
    )
    if len(prior_matches) != 1:
        errors.append("reuse_source_run_not_prior")
    if (
        source_run_id != str(candidate.get("source_run_id") or "")
        or str(source_authority.get("run_thread_id") or "")
        != lineage["thread_id"]
        or str(source_authority.get("run_topic_id") or "")
        != lineage["current_topic_id"]
        or str(source_authority.get("run_status") or "") != "completed"
    ):
        errors.append("reuse_source_candidate_owner_invalid")
    if (
        str(result_ref_record.get("topic_id") or "")
        != lineage["current_topic_id"]
        or str(result_ref_record.get("result_ref") or "") != source_ref
        or str(candidate.get("result_ref") or "") != source_ref
        or str(result_ref_record.get("snapshot_id") or "")
        != str(candidate.get("runtime_snapshot_id") or "")
        or str(result_ref_record.get("contract_version") or "")
        != str(candidate.get("runtime_contract_version") or "")
        or str(result_ref_record.get("permission_scope") or "")
        != str(candidate.get("permission_scope") or "")
        or str(result_ref_record.get("semantic_scope") or "")
        != str(candidate.get("semantic_scope_signature") or "")
    ):
        errors.append("reuse_source_candidate_index_invalid")

    source_contract = source_authority.get("analysis_contract")
    if not isinstance(source_contract, Mapping):
        errors.append("reuse_source_candidate_contract_invalid")
        return None, list(dict.fromkeys(errors))
    try:
        computed_source_signature = analysis_contract_signature(source_contract)
    except (KeyError, TypeError, ValueError):
        errors.append("reuse_source_candidate_contract_invalid")
        return None, list(dict.fromkeys(errors))
    source_contract_ref = str(source_contract.get("analysis_contract_id") or "")
    if (
        source_contract_ref != str(candidate.get("analysis_contract_ref") or "")
        or source_contract_ref == current_analysis_contract_ref
        or str(source_authority.get("stored_analysis_contract_signature") or "")
        != computed_source_signature
        or str(candidate.get("analysis_contract_signature") or "")
        != computed_source_signature
        or str(candidate.get("semantic_scope_signature") or "")
        != f"analysis-contract:sha256:{computed_source_signature}"
    ):
        errors.append("reuse_source_candidate_contract_invalid")

    source_request = source_authority.get("source_run_request")
    context_manifest = (
        source_request.get("context_manifest")
        if isinstance(source_request, Mapping)
        else None
    )
    contract_versions = (
        context_manifest.get("contract_versions")
        if isinstance(context_manifest, Mapping)
        else None
    )
    if (
        not isinstance(context_manifest, Mapping)
        or not isinstance(contract_versions, Mapping)
        or str(context_manifest.get("snapshot_version") or "")
        != str(candidate.get("runtime_snapshot_id") or "")
        or str(contract_versions.get("runtime") or "")
        != str(candidate.get("runtime_contract_version") or "")
    ):
        errors.append("reuse_source_candidate_runtime_context_invalid")

    try:
        query_by_record = evidence_resolver.resolve_query_execution_record(
            str(candidate["query_execution_record_ref"])
        )
        query_by_result = evidence_resolver.resolve_query_execution(source_ref)
    except Exception:
        query_by_record = None
        query_by_result = None
    if (
        type(query_by_record) is not QueryExecutionRecord
        or type(query_by_result) is not QueryExecutionRecord
        or query_by_record.record_ref != query_by_result.record_ref
        or runtime_evidence_record_integrity_errors(query_by_record)
        or query_by_record.record_digest
        != str(candidate.get("query_execution_record_digest") or "")
        or query_by_record.query_contract_ref
        != str(candidate.get("query_contract_ref") or "")
        or query_by_record.contract_signature
        != str(candidate.get("query_contract_signature") or "")
        or query_by_record.contract.analysis_contract_ref != source_contract_ref
        or query_by_record.result_ref != source_ref
        or query_by_record.rows_ref != str(candidate.get("rows_ref") or "")
        or query_by_record.completeness_report_ref
        != str(candidate.get("completeness_report_ref") or "")
    ):
        errors.append("reuse_source_candidate_query_invalid")
        return None, list(dict.fromkeys(errors))
    source_record = query_by_record

    try:
        rows_by_record = evidence_resolver.resolve_rows_record(
            str(candidate["rows_record_ref"])
        )
        rows_by_ref = evidence_resolver.resolve_rows(str(candidate["rows_ref"]))
    except Exception:
        rows_by_record = None
        rows_by_ref = None
    if (
        rows_by_record is None
        or rows_by_ref is None
        or rows_by_record.record_ref != rows_by_ref.record_ref
        or runtime_evidence_record_integrity_errors(rows_by_record)
        or rows_by_record.record_digest
        != str(candidate.get("rows_record_digest") or "")
        or rows_by_record.rows_ref != str(candidate.get("rows_ref") or "")
        or rows_by_record.rows_content_hash
        != str(candidate.get("rows_content_hash") or "")
        or rows_by_record.row_count != source_record.row_count
    ):
        errors.append("reuse_source_candidate_rows_invalid")

    snapshot_refs = tuple(candidate.get("source_snapshot_refs") or ())
    if (
        snapshot_refs != source_record.source_snapshot_refs
        or tuple(candidate.get("source_snapshot_record_refs") or ())
        != source_record.source_snapshot_record_refs
        or tuple(candidate.get("source_snapshot_record_digests") or ())
        != source_record.source_snapshot_record_digests
    ):
        errors.append("reuse_source_candidate_snapshot_invalid")
    release_method = getattr(release_resolver, "resolve_dataset_release", None)
    for index, snapshot_ref in enumerate(snapshot_refs):
        try:
            snapshot = evidence_resolver.resolve_snapshot(snapshot_ref)
            release = (
                release_method(snapshot.snapshot.release_ref)
                if snapshot is not None and callable(release_method)
                else None
            )
        except Exception:
            snapshot = None
            release = None
        if (
            snapshot is None
            or runtime_evidence_record_integrity_errors(snapshot)
            or snapshot.record_ref
            != candidate["source_snapshot_record_refs"][index]
            or snapshot.record_digest
            != candidate["source_snapshot_record_digests"][index]
            or snapshot.snapshot.release_ref != candidate["source_release_refs"][index]
            or snapshot.snapshot.authority_record_ref
            != candidate["source_release_authority_refs"][index]
            or snapshot.snapshot.schema_fingerprint
            != candidate["source_schema_fingerprints"][index]
            or release is None
            or release.authority_record_ref
            != candidate["source_release_authority_refs"][index]
            or release.integrity_errors
            or snapshot_ref not in release.snapshot_refs
        ):
            errors.append("reuse_source_candidate_snapshot_invalid")
            break

    candidate_completeness: dict[str, Any] = {}
    for record_ref, record_digest in zip(
        candidate.get("completeness_record_refs") or (),
        candidate.get("completeness_record_digests") or (),
    ):
        try:
            record = evidence_resolver.resolve_completeness(str(record_ref))
        except Exception:
            record = None
        if (
            record is None
            or runtime_evidence_record_integrity_errors(record)
            or record.report_digest != str(record_digest)
        ):
            errors.append("reuse_source_candidate_completeness_invalid")
            continue
        candidate_completeness[record.record_ref] = record

    matching_chains = []
    for binding_ref, binding_digest in zip(
        candidate.get("binding_record_refs") or (),
        candidate.get("binding_record_digests") or (),
    ):
        try:
            binding = evidence_resolver.resolve_capability_binding(str(binding_ref))
        except Exception:
            binding = None
        if (
            type(binding) is not CapabilityBindingRecord
            or binding.record_ref != str(binding_ref)
            or binding.binding_digest != str(binding_digest)
            or runtime_evidence_record_integrity_errors(binding)
            or binding.analysis_contract_ref != source_contract_ref
        ):
            errors.append("reuse_source_candidate_binding_invalid")
            continue
        try:
            chain = validate_authoritative_query_chain(
                binding,
                resolver=evidence_resolver,
                rows_loader=rows_loader,
                runtime_registry=registry,
                release_resolver=release_resolver,
            )
        except (AuthoritativeQueryChainError, EvidenceIntegrityError, TypeError, ValueError):
            errors.append("reuse_source_authoritative_query_chain_invalid")
            continue
        chain_reports = (*chain.primary_reports, *chain.validation_reports)
        if binding.status != "ready" or any(
            report.completeness_status != "complete"
            or report.analysis_readiness != "ready"
            for report in chain_reports
        ):
            errors.append("reuse_source_chain_not_claim_ready")
        if (
            binding.capability_id == expected_capability
            and source_ref in (*binding.result_refs, *binding.validation_result_refs)
            and source_ref in chain.query_records
        ):
            matching_chains.append((binding, chain))
    if len(matching_chains) != 1:
        errors.append("reuse_source_candidate_binding_invalid")
    else:
        _, source_chain = matching_chains[0]
        source_reports = (*source_chain.primary_reports, *source_chain.validation_reports)
        source_results = (*source_chain.primary_results, *source_chain.validation_results)
        matching_report = next(
            (
                report
                for result, report in zip(source_results, source_reports)
                if result.result_ref == source_ref
            ),
            None,
        )
        if (
            matching_report is None
            or matching_report.completeness_status != "complete"
            or matching_report.analysis_readiness != "ready"
            or not any(
                record.result_ref == source_ref
                and record.report_ref == matching_report.report_ref
                for record in candidate_completeness.values()
            )
        ):
            errors.append("reuse_source_chain_not_claim_ready")
    return source_record, list(dict.fromkeys(errors))


def _review_required_reuse_candidate(
    authority: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    registry: RuntimeContractRegistry,
    evidence_resolver: Any,
    rows_loader: Any,
    release_resolver: Any,
    conversation_store: Any,
    case_lineage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    review: dict[str, Any] = {
        "passed": False,
        "errors": [],
        "source_result_ref": "",
        "current_result_ref": "",
        "query_contract_ref": "",
        "capability_id": "",
        "dataset_ids": [],
    }
    errors: list[str] = review["errors"]
    expected_capability = str(expected.get("capability_id") or "")
    expected_datasets = frozenset(
        str(dataset_id) for dataset_id in expected.get("dataset_ids") or ()
    )
    if not expected_capability or not expected_datasets:
        errors.append("expected_reuse_provenance_missing")
        return review
    accepted_contract = _run_matched_accepted_analysis_contract(authority)
    admin = authority.get("admin_audit")
    if accepted_contract is None or not isinstance(admin, Mapping):
        errors.append("run_matched_reuse_authority_missing")
        return review
    normalized_lineage = _normalized_reuse_case_lineage(case_lineage)
    if normalized_lineage is None:
        errors.append("reuse_case_lineage_invalid")
        return review
    if str(authority.get("run_id") or "") != normalized_lineage["current_run_id"]:
        errors.append("reuse_current_case_lineage_mismatch")
    current_run_resolver = getattr(conversation_store, "get_run_request", None)
    try:
        current_run_request = (
            current_run_resolver(normalized_lineage["current_run_id"])
            if callable(current_run_resolver)
            else None
        )
    except Exception:
        current_run_request = None
    if (
        not isinstance(current_run_request, Mapping)
        or str(current_run_request.get("thread_id") or "")
        != normalized_lineage["thread_id"]
        or str(current_run_request.get("topic_id") or "")
        != normalized_lineage["current_topic_id"]
    ):
        errors.append("reuse_current_run_owner_invalid")
    raw_decisions = admin.get("reuse_decisions")
    if not isinstance(raw_decisions, (list, tuple)):
        errors.append("admin_reuse_decision_missing")
        return review
    decisions = tuple(
        item
        for item in raw_decisions
        if isinstance(item, Mapping) and item.get("decision") == "reuse"
    )
    if len(decisions) != 1:
        errors.append(
            "admin_reuse_decision_missing"
            if not decisions
            else "admin_reuse_decision_ambiguous"
        )
        return review
    if not isinstance(evidence_resolver, RuntimeEvidenceResolver):
        errors.append("runtime_evidence_resolver_invalid")
        return review
    if not isinstance(rows_loader, RowsPayloadLoader):
        errors.append("rows_payload_loader_invalid")
        return review

    decision = decisions[0]
    source_ref = str(decision.get("source_ref") or "")
    current_ref = str(decision.get("result_ref") or "")
    query_ref = str(decision.get("query_contract_ref") or "")
    review.update(
        source_result_ref=source_ref,
        current_result_ref=current_ref,
        query_contract_ref=query_ref,
    )
    if not source_ref or not current_ref or source_ref == current_ref:
        errors.append("reuse_source_current_result_alias")
    if str(decision.get("reason") or "") != "validated_authoritative_query_chain":
        errors.append("reuse_decision_reason_invalid")
    if (
        decision.get("can_support_claim") is not True
        or decision.get("requires_rerun") is not False
    ):
        errors.append("reuse_decision_flags_invalid")
    evidence_items = tuple(
        item
        for section in authority.get("sections") or ()
        if isinstance(section, Mapping)
        for payload in (section.get("payload"),)
        if isinstance(payload, Mapping)
        for item in payload.get("evidence") or ()
        if isinstance(item, Mapping)
    )
    bindings_by_ref: dict[str, CapabilityBindingRecord] = {}
    for evidence in evidence_items:
        binding_ref = str(evidence.get("binding_manifest_ref") or "")
        if not binding_ref:
            continue
        try:
            binding = evidence_resolver.resolve_capability_binding(binding_ref)
        except Exception:
            continue
        if type(binding) is not CapabilityBindingRecord:
            continue
        if (
            binding.record_ref == binding_ref
            and binding.binding_digest
            == str(evidence.get("binding_manifest_digest") or "")
            and binding.capability_id == expected_capability
            and current_ref
            in (*binding.result_refs, *binding.validation_result_refs)
            and current_ref
            in {
                str(ref) for ref in evidence.get("result_refs") or () if ref
            }
        ):
            bindings_by_ref[binding.record_ref] = binding
    bindings = tuple(bindings_by_ref.values())
    if bindings:
        review["capability_id"] = expected_capability
    if len(bindings) != 1:
        errors.append(
            "reuse_current_result_mismatch"
            if not bindings
            else "reuse_current_binding_ambiguous"
        )
        return review
    binding = bindings[0]
    review["capability_id"] = binding.capability_id
    if binding.analysis_contract_ref != accepted_contract.analysis_contract_id:
        errors.append("reuse_binding_run_owner_mismatch")
    if binding.status != "ready" or any(
        status != "complete" for status in binding.input_completeness_statuses
    ):
        errors.append("reuse_current_chain_not_claim_ready")
        return review
    try:
        chain = validate_authoritative_query_chain(
            binding,
            resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=registry,
            release_resolver=release_resolver,
        )
    except (AuthoritativeQueryChainError, EvidenceIntegrityError, TypeError, ValueError):
        errors.append("reuse_current_authoritative_query_chain_invalid")
        return review
    current_record = chain.query_records.get(current_ref)
    if type(current_record) is not QueryExecutionRecord:
        errors.append("reuse_current_result_mismatch")
        return review
    current_reports = (*chain.primary_reports, *chain.validation_reports)
    if any(
        record.contract.analysis_contract_ref
        != accepted_contract.analysis_contract_id
        or str(record.query_contract.get("analysis_contract_ref") or "")
        != accepted_contract.analysis_contract_id
        for record in chain.query_records.values()
    ):
        errors.append("reuse_current_query_contract_owner_invalid")
    if (
        current_record.execution_status != "succeeded"
        or any(
            report.completeness_status != "complete"
            or report.analysis_readiness != "ready"
            for report in current_reports
        )
    ):
        errors.append("reuse_current_chain_not_claim_ready")
        return review
    if current_record.query_contract_ref != query_ref:
        errors.append("reuse_query_contract_mismatch")
    stats = current_record.result_payload.get("provider_stats") or {}
    if not isinstance(stats, Mapping):
        stats = {}
    if (
        stats.get("cache_hit") is not True
        or stats.get("cache_source") != "validated_authoritative_query_chain"
        or str(stats.get("source_result_ref") or "") != source_ref
    ):
        errors.append("reuse_source_result_mismatch")
    source_record, source_candidate_errors = _review_published_source_candidate(
        source_ref=source_ref,
        expected_capability=expected_capability,
        current_analysis_contract_ref=accepted_contract.analysis_contract_id,
        registry=registry,
        evidence_resolver=evidence_resolver,
        rows_loader=rows_loader,
        release_resolver=release_resolver,
        conversation_store=conversation_store,
        case_lineage=normalized_lineage,
        decision_candidate_signature=str(
            decision.get("candidate_signature") or ""
        ),
        current_candidate_signature=str(stats.get("candidate_signature") or ""),
    )
    errors.extend(source_candidate_errors)
    if (
        type(source_record) is not QueryExecutionRecord
        or source_record.result_ref != source_ref
        or runtime_evidence_record_integrity_errors(source_record)
        or source_record.execution_status != "succeeded"
    ):
        errors.append("reuse_source_result_authority_invalid")
    else:
        if (
            source_record.contract_signature != current_record.contract_signature
            or source_record.query_hash != current_record.query_hash
            or source_record.source_snapshot_refs
            != current_record.source_snapshot_refs
            or source_record.contract.permission_scope
            != current_record.contract.permission_scope
        ):
            errors.append("reuse_source_query_material_mismatch")
        if (
            source_record.rows_content_hash != current_record.rows_content_hash
            or source_record.row_count != current_record.row_count
        ):
            errors.append("reuse_source_rows_mismatch")
    dataset_ids: list[str] = []
    for snapshot_ref in current_record.source_snapshot_refs:
        try:
            snapshot = evidence_resolver.resolve_snapshot(snapshot_ref)
        except Exception:
            snapshot = None
        if snapshot is None:
            errors.append("reuse_snapshot_authority_missing")
            continue
        dataset_ids.append(str(snapshot.snapshot.dataset_id or ""))
    review["dataset_ids"] = sorted(dict.fromkeys(dataset_ids))
    if frozenset(review["dataset_ids"]) != expected_datasets:
        errors.append("reuse_dataset_provenance_mismatch")
    review["errors"] = list(dict.fromkeys(errors))
    review["passed"] = not review["errors"]
    return review


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
    requires_claims = _expectation_requires_claims(expect)
    claim_review = _claim_evidence_review(
        effective_result.get("answer_package") or {},
        manifest if isinstance(manifest, dict) else {},
        requires_claims=requires_claims,
    )
    raw_manifest_claim_support = (
        manifest.get("can_support_claims")
        if isinstance(manifest, dict)
        else None
    )
    manifest_can_support_claims = raw_manifest_claim_support is True
    legal_zero_claim_terminal = (
        not requires_claims
        and claim_review["claim_count"] == 0
        and raw_manifest_claim_support is False
    )
    claim_support_ok = (
        manifest_present
        and claim_review["passed"]
        and (
            (
                claim_review["claim_count"] > 0
                and manifest_can_support_claims
            )
            or legal_zero_claim_terminal
        )
    )
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


_QUALITY_CODE_LIST_FIELDS = (
    "issues",
    "repairable_warnings",
    "final_summary_display_warnings",
    "risk_flags",
)
_QUALITY_BOOLEAN_FIELDS = (
    "blocks_display",
    "direct_answer",
    "has_verified_claims",
    "verified_claim_preserved",
    "business_insight_present",
    "followups_one_intent",
)
_REQUIRED_QUALITY_BOOLEAN_FIELDS = (
    "direct_answer",
    "business_insight_present",
    "followups_one_intent",
    "has_verified_claims",
    "verified_claim_preserved",
)


def _quality_code_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _has_valid_quality_projection(answer_package: Mapping[str, Any]) -> bool:
    quality_gate = answer_package.get("quality_gate")
    if not isinstance(quality_gate, Mapping) or not quality_gate:
        return False
    if any(field not in quality_gate for field in _REQUIRED_QUALITY_BOOLEAN_FIELDS):
        return False
    if "repairable_warnings" not in quality_gate:
        return False
    for field in _QUALITY_CODE_LIST_FIELDS:
        if field not in quality_gate:
            continue
        value = quality_gate[field]
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            return False
    for field in _QUALITY_BOOLEAN_FIELDS:
        if field in quality_gate and not isinstance(quality_gate[field], bool):
            return False
    if "display_status" in quality_gate and not isinstance(
        quality_gate["display_status"], str
    ):
        return False
    return True


def _quality_review(answer_package: dict[str, Any]) -> dict[str, Any]:
    quality_gate = answer_package.get("quality_gate") if isinstance(answer_package, dict) else {}
    if not isinstance(quality_gate, Mapping):
        quality_gate = {}
    issues = _quality_code_list(quality_gate.get("issues"))
    final_summary_warnings = _quality_code_list(
        quality_gate.get("final_summary_display_warnings")
    )
    repairable_warnings = _quality_code_list(
        quality_gate.get("repairable_warnings")
    )
    soft_warnings = list(
        dict.fromkeys(
            [
                str(item)
                for item in (
                    *issues,
                    *repairable_warnings,
                    *final_summary_warnings,
                )
                if item
            ]
        )
    )
    return {
        "blocks_display": quality_gate.get("blocks_display") is True,
        "display_status": (
            quality_gate.get("display_status")
            if isinstance(quality_gate.get("display_status"), str)
            else ""
        ),
        "final_answer_audit_warnings": repairable_warnings,
        "quality_gate_issues": issues,
        "final_summary_display_warnings": final_summary_warnings,
        "quality_warnings": soft_warnings,
        "risk_markers": _quality_code_list(quality_gate.get("risk_flags")),
        "direct_answer": quality_gate.get("direct_answer") is True,
        "has_verified_claims": quality_gate.get("has_verified_claims") is True,
        "verified_claim_preserved": (
            quality_gate.get("verified_claim_preserved") is True
        ),
        "business_insight_present": (
            quality_gate.get("business_insight_present") is True
        ),
        "followups_one_intent": quality_gate.get("followups_one_intent") is True,
    }


def _load_run_matched_internal_answer_package(
    result: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any] | None:
    raw_path = result.get("artifact_path")
    expected_run_id = result.get("run_id")
    if (
        not isinstance(raw_path, str)
        or not raw_path.strip()
        or not isinstance(expected_run_id, str)
        or not expected_run_id
    ):
        return None
    raw = Path(raw_path)
    if ".." in raw.parts:
        return None
    try:
        trusted_root = (artifact_root or (ROOT / "artifacts")).resolve(strict=True)
        candidate = (
            raw
            if raw.is_absolute()
            else (trusted_root / raw if artifact_root is not None else ROOT / raw)
        )
        internal_path = candidate.resolve(strict=True)
        internal_path.relative_to(trusted_root)
        internal_package = json.loads(internal_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(internal_package, dict):
        return None
    persisted_run_id = internal_package.get("run_id")
    if not isinstance(persisted_run_id, str) or not persisted_run_id:
        return None
    if persisted_run_id != expected_run_id:
        return None
    return internal_package


def _runtime_quality_review(
    result: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    internal_package = _load_run_matched_internal_answer_package(
        result,
        artifact_root=artifact_root,
    )
    if internal_package is not None:
        if _has_valid_quality_projection(internal_package):
            return _quality_review(internal_package)
    public_package = result.get("answer_package") or {}
    return _quality_review(public_package if isinstance(public_package, dict) else {})


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
    # Answer-quality findings stay visible in their own review artifact. They
    # do not decide suite acceptance; runtime and obligation contracts do.
    return False


def _real_clickhouse_review(
    result: dict[str, Any],
    *,
    real_clickhouse: bool,
    evidence_resolver: Any = None,
    required_datasets: tuple[str, ...] | list[str] = (),
    analysis_context: Mapping[str, Any] | None = None,
    runtime_authority_resolver: Any = None,
    runtime_authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    package = (
        dict(runtime_authority)
        if isinstance(runtime_authority, Mapping)
        else _runtime_audit_package(
            result,
            authority_resolver=runtime_authority_resolver,
        )
    )

    issues: list[str] = []
    authority_error = str(package.get("_authority_error") or "")
    if authority_error:
        issues.append(f"runtime_authority_error:{authority_error}")
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
    claim_authority_available = all(
        callable(getattr(evidence_resolver, method, None))
        for method in ("resolve_verified_claim", "resolve_claim_provenance")
    )
    if verified_claims and not claim_authority_available:
        issues.append("missing_verified_claim_authority_resolver")
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
            if not claim_authority_available:
                raise EvidenceIntegrityError(
                    "verified_claim_authority_resolver_missing"
                )
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
        except Exception as exc:
            provenance_complete = False
            if claim_authority_available:
                issues.append(
                    f"verified_claim_authority_error:{type(exc).__name__}"
                )
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


def _runtime_authority_resolver_for_store(conversation_store: Any):
    """Build the eval-only run resolver over normalized runtime authority."""
    if conversation_store is None:
        return None

    def resolve(run_id: str) -> dict[str, Any] | None:
        publications = getattr(
            conversation_store,
            "analysis_runtime_records",
            None,
        )
        if isinstance(publications, Mapping):
            publication = publications.get(run_id)
            if not isinstance(publication, Mapping):
                return None
            payload = publication.get("payload")
            if not isinstance(payload, Mapping):
                raise ValueError("runtime_authority_publication_invalid")
            run = getattr(conversation_store, "runs", {}).get(run_id)
            if not isinstance(run, Mapping):
                raise ValueError("runtime_evaluation_run_missing")
            audit_events = getattr(conversation_store, "audit_events", ())
            runtime_events = [
                event
                for event in audit_events
                if isinstance(event, Mapping) and event.get("run_id") == run_id
            ]
            contract = payload.get("analysis_contract")
            contract_ref = str(
                contract.get("analysis_contract_id")
                if isinstance(contract, Mapping)
                else ""
            )
            indexed = getattr(
                conversation_store,
                "analysis_runtime_authority",
                None,
            )
            indexed_contracts = (
                indexed.get("analysis_contract")
                if isinstance(indexed, Mapping)
                else None
            )
            indexed_contract = (
                indexed_contracts.get(contract_ref)
                if isinstance(indexed_contracts, Mapping)
                else None
            )
            digest = str(publication.get("digest") or "")
            delivery_verifier = _validate_runtime_evaluation_authority_owner_index(
                run_id=run_id,
                thread_id=str(run.get("thread_id") or ""),
                topic_id=str(run.get("topic_id") or ""),
                run_status=str(run.get("status") or ""),
                publication_digest=digest,
                bundle=payload,
                indexed_analysis_contract=indexed_contract,
                stored_contract_signature=str(
                    indexed_contract.get("contract_signature")
                    if isinstance(indexed_contract, Mapping)
                    else ""
                ),
                publication_events=[
                    event
                    for event in runtime_events
                    if event.get("event_type")
                    == "analysis_runtime_records_persisted"
                ],
                delivery_events=[
                    event
                    for event in runtime_events
                    if event.get("event_type") == "delivery_verifier_completed"
                ],
            )
            return _normalized_runtime_evaluation_projection(
                run_id=run_id,
                thread_id=str(run.get("thread_id") or ""),
                topic_id=str(run.get("topic_id") or ""),
                turn_id=str(run.get("turn_id") or ""),
                run_status=str(run.get("status") or ""),
                publication_digest=digest,
                bundle=payload,
                stored_contract_signature=str(
                    indexed_contract.get("contract_signature") or ""
                ),
                delivery_verifier=delivery_verifier,
            )

        fetchall = getattr(conversation_store, "_fetchall", None)
        if not callable(fetchall):
            return None
        rows = fetchall(
            """
            /* live_eval_runtime_evaluation_authority_root */
            SELECT r.run_id,
                   r.thread_id,
                   r.turn_id,
                   r.topic_id,
                   r.status AS run_status,
                   p.run_id AS publication_run_id,
                   p.topic_id AS publication_topic_id,
                   p.bundle_digest AS publication_digest,
                   p.payload AS publication_index,
                   ac.run_id AS indexed_contract_run_id,
                   ac.contract_signature AS stored_contract_signature,
                   ac.payload AS indexed_analysis_contract,
                   (
                     SELECT count(*)
                     FROM waje_runtime.analysis_contracts owned
                     WHERE owned.run_id = r.run_id
                   ) AS analysis_contract_count,
                   COALESCE((
                     SELECT jsonb_agg(
                       jsonb_build_object(
                         'event_type', event.event_type,
                         'thread_id', event.thread_id,
                         'topic_id', event.topic_id,
                         'run_id', event.run_id,
                         'ref', event.ref,
                         'payload', event.payload
                       ) ORDER BY event.audit_id
                     )
                     FROM waje_runtime.audit_events event
                     WHERE event.run_id = r.run_id
                       AND event.event_type = 'analysis_runtime_records_persisted'
                   ), '[]'::jsonb) AS publication_events,
                   COALESCE((
                     SELECT jsonb_agg(
                       jsonb_build_object(
                         'event_type', event.event_type,
                         'thread_id', event.thread_id,
                         'topic_id', event.topic_id,
                         'run_id', event.run_id,
                         'ref', event.ref,
                         'payload', event.payload
                       ) ORDER BY event.audit_id
                     )
                     FROM waje_runtime.audit_events event
                     WHERE event.run_id = r.run_id
                       AND event.event_type = 'delivery_verifier_completed'
                   ), '[]'::jsonb) AS delivery_verifier_events
            FROM waje_runtime.analysis_runs r
            LEFT JOIN waje_runtime.analysis_runtime_publications p
              ON p.run_id = r.run_id
            LEFT JOIN waje_runtime.analysis_contracts ac
              ON ac.analysis_contract_id = p.analysis_contract_id
            WHERE r.run_id = %(run_id)s
            """,
            {"run_id": run_id},
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("runtime_authority_contract_ambiguous")
        row = rows[0]
        resolved_run_id = _runtime_evaluation_row_field(row, "run_id", 0)
        thread_id = _runtime_evaluation_row_field(row, "thread_id", 1)
        turn_id = _runtime_evaluation_row_field(row, "turn_id", 2)
        topic_id = _runtime_evaluation_row_field(row, "topic_id", 3)
        run_status = _runtime_evaluation_row_field(row, "run_status", 4)
        publication_run_id = _runtime_evaluation_row_field(
            row, "publication_run_id", 5
        )
        publication_topic_id = _runtime_evaluation_row_field(
            row, "publication_topic_id", 6
        )
        publication_digest = _runtime_evaluation_row_field(
            row, "publication_digest", 7
        )
        publication_index = _runtime_evaluation_json_value(
            _runtime_evaluation_row_field(row, "publication_index", 8)
        )
        indexed_contract_run_id = _runtime_evaluation_row_field(
            row, "indexed_contract_run_id", 9
        )
        stored_signature = _runtime_evaluation_row_field(
            row, "stored_contract_signature", 10
        )
        indexed_contract = _runtime_evaluation_json_value(
            _runtime_evaluation_row_field(
                row,
                "indexed_analysis_contract",
                11,
            )
        )
        contract_count = _runtime_evaluation_row_field(
            row, "analysis_contract_count", 12
        )
        publication_events = _runtime_evaluation_json_value(
            _runtime_evaluation_row_field(row, "publication_events", 13)
        )
        delivery_events = _runtime_evaluation_json_value(
            _runtime_evaluation_row_field(row, "delivery_verifier_events", 14)
        )
        if contract_count != 1:
            raise ValueError("runtime_authority_contract_ambiguous")
        resolved_run_id = str(resolved_run_id or "")
        thread_id = str(thread_id or "")
        topic_id = str(topic_id or "")
        if str(run_status or "") != "completed":
            raise ValueError("runtime_evaluation_run_incomplete")
        if (
            not resolved_run_id
            or str(publication_run_id or "") != resolved_run_id
            or str(publication_topic_id or "") != topic_id
            or str(indexed_contract_run_id or "") != resolved_run_id
        ):
            raise ValueError("runtime_evaluation_authority_cross_run")
        validated_index = _validated_runtime_evaluation_publication_index(
            publication_index,
            indexed_analysis_contract=indexed_contract,
        )
        ordered_refs = validated_index["ordered_refs"]
        inventory_rows = fetchall(
            _runtime_evaluation_inventory_sql(),
            {
                "run_id": resolved_run_id,
                "ordered_refs": json.dumps(
                    ordered_refs,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )
        bundle = _runtime_evaluation_bundle_from_inventory(
            run_id=resolved_run_id,
            indexed_analysis_contract=indexed_contract,
            ordered_refs=ordered_refs,
            inventory_rows=inventory_rows,
        )
        delivery_verifier = _validate_runtime_evaluation_authority_owner_index(
            run_id=resolved_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            run_status=str(run_status or ""),
            publication_digest=str(publication_digest or ""),
            bundle=bundle,
            indexed_analysis_contract=indexed_contract,
            stored_contract_signature=str(stored_signature or ""),
            publication_events=publication_events,
            delivery_events=delivery_events,
        )
        return _normalized_runtime_evaluation_projection(
            run_id=resolved_run_id,
            thread_id=thread_id,
            topic_id=topic_id,
            turn_id=str(turn_id or ""),
            run_status=str(run_status or ""),
            publication_digest=str(publication_digest or ""),
            bundle=bundle,
            stored_contract_signature=str(stored_signature or ""),
            delivery_verifier=delivery_verifier,
        )

    return resolve


def _runtime_evaluation_row_field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _runtime_evaluation_json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("runtime_evaluation_authority_json_invalid") from exc
    return value


def _validated_runtime_evaluation_publication_index(
    publication_index: Any,
    *,
    indexed_analysis_contract: Any,
) -> dict[str, Any]:
    if (
        not isinstance(publication_index, Mapping)
        or set(publication_index)
        != {"schema_version", "analysis_contract_id", "ordered_refs"}
        or publication_index.get("schema_version")
        != RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION
        or not isinstance(indexed_analysis_contract, Mapping)
    ):
        raise ValueError("runtime_evaluation_publication_index_invalid")
    if str(publication_index.get("analysis_contract_id") or "") != str(
        indexed_analysis_contract.get("analysis_contract_id") or ""
    ):
        raise ValueError("runtime_evaluation_authority_index_mismatch")
    raw_groups = publication_index.get("ordered_refs")
    if (
        not isinstance(raw_groups, Mapping)
        or set(raw_groups) != set(RUNTIME_PUBLICATION_RECORD_GROUPS)
    ):
        raise ValueError("runtime_evaluation_publication_index_invalid")
    ordered_refs: dict[str, list[str]] = {}
    for group in RUNTIME_PUBLICATION_RECORD_GROUPS:
        refs = raw_groups.get(group)
        if (
            not isinstance(refs, list)
            or any(not isinstance(ref, str) or not ref for ref in refs)
            or len(refs) != len(set(refs))
        ):
            raise ValueError("runtime_evaluation_publication_index_invalid")
        ordered_refs[group] = list(refs)
    return {
        "schema_version": RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION,
        "analysis_contract_id": str(publication_index["analysis_contract_id"]),
        "ordered_refs": ordered_refs,
    }


def _runtime_evaluation_bundle_from_inventory(
    *,
    run_id: str,
    indexed_analysis_contract: Any,
    ordered_refs: Mapping[str, list[str]],
    inventory_rows: Any,
) -> dict[str, Any]:
    if not isinstance(indexed_analysis_contract, Mapping):
        raise ValueError("runtime_evaluation_authority_index_mismatch")
    if not isinstance(inventory_rows, (list, tuple)):
        raise ValueError("runtime_evaluation_normalized_rows_invalid")
    wrapped_kinds = {
        "query_execution_records": "query_execution",
        "rows_records": "rows",
        "snapshot_records": "snapshot",
        "completeness_records": "completeness",
        "capability_binding_records": "capability_binding",
    }
    records_by_group: dict[str, dict[str, Mapping[str, Any]]] = {
        group: {} for group in RUNTIME_PUBLICATION_RECORD_GROUPS
    }
    for row in inventory_rows:
        group = str(_runtime_evaluation_row_field(row, "record_group", 0) or "")
        record_ref = str(
            _runtime_evaluation_row_field(row, "record_ref", 1) or ""
        )
        owner_run_ids = _runtime_evaluation_json_value(
            _runtime_evaluation_row_field(row, "owner_run_ids", 2)
        )
        raw_payload = _runtime_evaluation_json_value(
            _runtime_evaluation_row_field(row, "payload", 3)
        )
        if group not in records_by_group or not record_ref:
            raise ValueError("runtime_evaluation_normalized_rows_invalid")
        if (
            not isinstance(owner_run_ids, list)
            or not owner_run_ids
            or any(str(owner or "") != run_id for owner in owner_run_ids)
        ):
            raise ValueError("runtime_evaluation_authority_cross_run")
        if group in wrapped_kinds:
            if (
                not isinstance(raw_payload, Mapping)
                or set(raw_payload) != {"kind", "record"}
                or raw_payload.get("kind") != wrapped_kinds[group]
                or not isinstance(raw_payload.get("record"), Mapping)
            ):
                raise ValueError("runtime_evaluation_normalized_rows_invalid")
            payload = raw_payload["record"]
        else:
            if not isinstance(raw_payload, Mapping):
                raise ValueError("runtime_evaluation_normalized_rows_invalid")
            payload = raw_payload
        try:
            payload_ref = runtime_publication_record_ref(group, payload)
        except (EvidenceIntegrityError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("runtime_evaluation_normalized_rows_invalid") from exc
        if payload_ref != record_ref:
            raise ValueError("runtime_evaluation_normalized_rows_invalid")
        group_records = records_by_group[group]
        if record_ref in group_records:
            raise ValueError("runtime_evaluation_normalized_rows_ambiguous")
        group_records[record_ref] = dict(payload)

    bundle: dict[str, Any] = {
        "analysis_contract": dict(indexed_analysis_contract),
    }
    for group in RUNTIME_PUBLICATION_RECORD_GROUPS:
        expected = ordered_refs[group]
        records = records_by_group[group]
        missing = set(expected) - set(records)
        unexpected = set(records) - set(expected)
        if missing:
            raise ValueError("runtime_evaluation_normalized_rows_missing")
        if unexpected:
            raise ValueError("runtime_evaluation_normalized_rows_unexpected")
        bundle[group] = [records[ref] for ref in expected]
    return bundle


def _runtime_evaluation_inventory_sql() -> str:
    return """
        /* live_eval_runtime_evaluation_authority_inventory */
        WITH requested AS (
          SELECT %(run_id)s::text AS run_id,
                 %(ordered_refs)s::jsonb AS ordered_refs
        )
        SELECT 'query_contracts' AS record_group,
               query_contract.query_contract_id AS record_ref,
               jsonb_build_array(query_contract.run_id) AS owner_run_ids,
               query_contract.payload
        FROM requested
        JOIN waje_runtime.query_contracts query_contract
          ON query_contract.run_id = requested.run_id
          OR query_contract.query_contract_id IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'query_contracts'
            )
          )
        UNION ALL
        SELECT 'query_execution_records',
               execution.record_ref,
               jsonb_build_array(
                 execution.run_id, query_run.run_id, query_contract.run_id
               ),
               execution.payload
        FROM requested
        JOIN waje_runtime.query_execution_authority execution
          ON execution.run_id = requested.run_id
          OR execution.record_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'query_execution_records'
            )
          )
        LEFT JOIN waje_runtime.query_runs query_run
          ON query_run.result_ref = execution.result_ref
        LEFT JOIN waje_runtime.query_contracts query_contract
          ON query_contract.query_contract_id = execution.query_contract_ref
        UNION ALL
        SELECT 'rows_records',
               rows_record.record_ref,
               COALESCE((
                 SELECT jsonb_agg(DISTINCT linked.run_id ORDER BY linked.run_id)
                 FROM waje_runtime.query_execution_authority linked
                 WHERE linked.rows_ref = rows_record.rows_ref
                   AND (
                     linked.run_id = requested.run_id
                     OR linked.record_ref IN (
                       SELECT jsonb_array_elements_text(
                         requested.ordered_refs -> 'query_execution_records'
                       )
                     )
                   )
               ), '[]'::jsonb),
               rows_record.payload
        FROM requested
        JOIN waje_runtime.rows_metadata_authority rows_record
          ON rows_record.record_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'rows_records'
            )
          )
          OR EXISTS (
            SELECT 1
            FROM waje_runtime.query_execution_authority current_execution
            WHERE current_execution.run_id = requested.run_id
              AND current_execution.rows_ref = rows_record.rows_ref
          )
        UNION ALL
        SELECT 'snapshot_records',
               snapshot_record.record_ref,
               COALESCE((
                 SELECT jsonb_agg(DISTINCT linked.run_id ORDER BY linked.run_id)
                 FROM (
                   SELECT execution.run_id
                   FROM waje_runtime.query_execution_authority execution
                   WHERE (
                       execution.run_id = requested.run_id
                       OR execution.record_ref IN (
                         SELECT jsonb_array_elements_text(
                           requested.ordered_refs -> 'query_execution_records'
                         )
                       )
                     )
                     AND EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements_text(
                         COALESCE(
                           execution.payload #> '{record,source_snapshot_record_refs}',
                           '[]'::jsonb
                         )
                       ) AS source_record(record_ref)
                       WHERE source_record.record_ref = snapshot_record.record_ref
                     )
                   UNION
                   SELECT query_contract.run_id
                   FROM waje_runtime.query_contracts query_contract
                   WHERE (
                       query_contract.run_id = requested.run_id
                       OR query_contract.query_contract_id IN (
                         SELECT jsonb_array_elements_text(
                           requested.ordered_refs -> 'query_contracts'
                         )
                       )
                     )
                     AND COALESCE(
                       query_contract.payload -> 'dataset_snapshot_refs',
                       '[]'::jsonb
                     ) ? snapshot_record.snapshot_ref
                 ) linked
               ), '[]'::jsonb),
               snapshot_record.payload
        FROM requested
        JOIN waje_runtime.snapshot_authority snapshot_record
          ON snapshot_record.record_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'snapshot_records'
            )
          )
          OR EXISTS (
            SELECT 1
            FROM waje_runtime.query_execution_authority current_execution
            WHERE current_execution.run_id = requested.run_id
              AND EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(
                  COALESCE(
                    current_execution.payload
                      #> '{record,source_snapshot_record_refs}',
                    '[]'::jsonb
                  )
                ) AS source_record(record_ref)
                WHERE source_record.record_ref = snapshot_record.record_ref
              )
          )
          OR EXISTS (
            SELECT 1
            FROM waje_runtime.query_contracts current_contract
            WHERE current_contract.run_id = requested.run_id
              AND COALESCE(
                current_contract.payload -> 'dataset_snapshot_refs',
                '[]'::jsonb
              ) ? snapshot_record.snapshot_ref
          )
        UNION ALL
        SELECT 'completeness_records',
               completeness.record_ref,
               jsonb_build_array(
                 completeness.run_id, query_run.run_id, query_contract.run_id
               ),
               completeness.payload
        FROM requested
        JOIN waje_runtime.query_completeness_reports completeness
          ON completeness.run_id = requested.run_id
          OR completeness.record_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'completeness_records'
            )
          )
        LEFT JOIN waje_runtime.query_runs query_run
          ON query_run.result_ref = completeness.result_ref
        LEFT JOIN waje_runtime.query_contracts query_contract
          ON query_contract.query_contract_id = completeness.query_contract_ref
        UNION ALL
        SELECT 'capability_binding_records',
               binding.record_ref,
               jsonb_build_array(binding.run_id, analysis_contract.run_id),
               binding.payload
        FROM requested
        JOIN waje_runtime.capability_binding_authority binding
          ON binding.run_id = requested.run_id
          OR binding.record_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'capability_binding_records'
            )
          )
        LEFT JOIN waje_runtime.analysis_contracts analysis_contract
          ON analysis_contract.analysis_contract_id = binding.analysis_contract_id
        UNION ALL
        SELECT 'evidence_manifests',
               evidence.evidence_ref,
               jsonb_build_array(evidence.run_id, binding.run_id),
               evidence.payload
        FROM requested
        JOIN waje_runtime.evidence_manifests evidence
          ON evidence.run_id = requested.run_id
          OR evidence.evidence_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'evidence_manifests'
            )
          )
        LEFT JOIN waje_runtime.capability_binding_authority binding
          ON binding.record_ref = evidence.binding_record_ref
        UNION ALL
        SELECT 'context_manifests',
               context.manifest_id,
               jsonb_build_array(context.run_id),
               context.payload
        FROM requested
        JOIN waje_runtime.context_manifests context
          ON context.run_id = requested.run_id
          OR context.manifest_id IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'context_manifests'
            )
          )
        UNION ALL
        SELECT 'trusted_provenance_records',
               provenance.record_ref,
               jsonb_build_array(provenance.run_id),
               provenance.payload
        FROM requested
        JOIN waje_runtime.claim_provenance_records provenance
          ON provenance.run_id = requested.run_id
          OR provenance.record_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'trusted_provenance_records'
            )
          )
        UNION ALL
        SELECT 'verified_claims',
               claim.claim_ref,
               jsonb_build_array(
                 claim.run_id, context.run_id, provenance.run_id
               ),
               claim.payload
        FROM requested
        JOIN waje_runtime.verified_claims claim
          ON claim.run_id = requested.run_id
          OR claim.claim_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'verified_claims'
            )
          )
        LEFT JOIN waje_runtime.context_manifests context
          ON context.manifest_id = claim.context_manifest_ref
        LEFT JOIN waje_runtime.claim_provenance_records provenance
          ON provenance.record_ref = claim.provenance_record_ref
        UNION ALL
        SELECT 'claim_links',
               link.claim_ref || chr(31) || link.evidence_ref,
               jsonb_build_array(claim.run_id, evidence.run_id, context.run_id),
               link.payload
        FROM requested
        JOIN waje_runtime.claim_evidence_links link
          ON link.claim_ref || chr(31) || link.evidence_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'claim_links'
            )
          )
          OR EXISTS (
            SELECT 1
            FROM waje_runtime.verified_claims current_claim
            WHERE current_claim.claim_ref = link.claim_ref
              AND current_claim.run_id = requested.run_id
          )
          OR EXISTS (
            SELECT 1
            FROM waje_runtime.evidence_manifests current_evidence
            WHERE current_evidence.evidence_ref = link.evidence_ref
              AND current_evidence.run_id = requested.run_id
          )
        LEFT JOIN waje_runtime.verified_claims claim
          ON claim.claim_ref = link.claim_ref
        LEFT JOIN waje_runtime.evidence_manifests evidence
          ON evidence.evidence_ref = link.evidence_ref
        LEFT JOIN waje_runtime.context_manifests context
          ON context.manifest_id = link.context_manifest_ref
        UNION ALL
        SELECT 'repair_attempts',
               repair.attempt_ref,
               jsonb_build_array(repair.run_id),
               repair.payload
        FROM requested
        JOIN waje_runtime.query_repair_attempts repair
          ON repair.run_id = requested.run_id
          OR repair.attempt_ref IN (
            SELECT jsonb_array_elements_text(
              requested.ordered_refs -> 'repair_attempts'
            )
          )
        ORDER BY record_group, record_ref
    """


def _unique_runtime_evaluation_event(
    events: Any,
    *,
    event_type: str,
    run_id: str,
    thread_id: str,
    topic_id: str,
    require_owner: bool = True,
) -> Mapping[str, Any]:
    if not isinstance(events, list):
        raise ValueError("runtime_evaluation_authority_event_invalid")
    matches = tuple(
        event
        for event in events
        if isinstance(event, Mapping) and event.get("event_type") == event_type
    )
    if len(matches) != 1:
        suffix = "missing" if not matches else "ambiguous"
        raise ValueError(f"runtime_evaluation_{event_type}_{suffix}")
    event = matches[0]
    if (
        str(event.get("run_id") or "") != run_id
        or require_owner
        and (
            str(event.get("thread_id") or "") != thread_id
            or str(event.get("topic_id") or "") != topic_id
        )
    ):
        raise ValueError("runtime_evaluation_authority_cross_run")
    return event


def _validate_runtime_evaluation_authority_owner_index(
    *,
    run_id: str,
    thread_id: str,
    topic_id: str,
    run_status: str,
    publication_digest: str,
    bundle: Any,
    indexed_analysis_contract: Any,
    stored_contract_signature: str,
    publication_events: Any,
    delivery_events: Any,
) -> Mapping[str, Any]:
    """Validate the exact persisted owner/index chain for live evaluation."""
    if not run_id or not thread_id or not topic_id:
        raise ValueError("runtime_evaluation_authority_owner_missing")
    if run_status != "completed":
        raise ValueError("runtime_evaluation_run_incomplete")
    if not isinstance(bundle, Mapping):
        raise ValueError("runtime_evaluation_authority_invalid")
    if not publication_digest or canonical_digest(bundle) != publication_digest:
        raise ValueError("runtime_evaluation_publication_digest_mismatch")

    publication_event = _unique_runtime_evaluation_event(
        publication_events,
        event_type="analysis_runtime_records_persisted",
        run_id=run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        require_owner=False,
    )
    publication_event_payload = publication_event.get("payload")
    if (
        not isinstance(publication_event_payload, Mapping)
        or str(publication_event_payload.get("bundle_digest") or "")
        != publication_digest
    ):
        raise ValueError("runtime_evaluation_publication_digest_mismatch")

    contract = bundle.get("analysis_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("runtime_authority_contract_missing")
    if (
        not isinstance(indexed_analysis_contract, Mapping)
        or canonical_value(indexed_analysis_contract) != canonical_value(contract)
    ):
        raise ValueError("runtime_evaluation_authority_index_mismatch")
    contract_signature = str(contract.get("contract_signature") or "")
    indexed_signature = str(
        indexed_analysis_contract.get("contract_signature") or ""
    )
    contract_payload = {
        key: value
        for key, value in contract.items()
        if key != "contract_signature"
    }
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("runtime_evaluation_analysis_contract_invalid") from exc
    if (
        not str(contract.get("analysis_contract_id") or "")
        or not contract_signature
        or stored_contract_signature != contract_signature
        or indexed_signature != contract_signature
        or analysis_contract_signature(typed_contract) != contract_signature
    ):
        raise ValueError("runtime_evaluation_analysis_contract_digest_mismatch")

    delivery_event = _unique_runtime_evaluation_event(
        delivery_events,
        event_type="delivery_verifier_completed",
        run_id=run_id,
        thread_id=thread_id,
        topic_id=topic_id,
    )
    verifier = delivery_event.get("payload")
    if (
        not isinstance(verifier, Mapping)
        or verifier.get("status") != "passed"
        or not isinstance(verifier.get("errors"), (list, tuple))
        or verifier.get("errors")
    ):
        raise ValueError("runtime_evaluation_delivery_verifier_rejected")
    return verifier


def _normalized_runtime_evaluation_projection(
    *,
    run_id: str,
    thread_id: str,
    topic_id: str,
    turn_id: str,
    run_status: str,
    publication_digest: str,
    bundle: Mapping[str, Any],
    stored_contract_signature: str,
    delivery_verifier: Any,
) -> dict[str, Any]:
    """Validate and normalize the persisted authority consumed by live eval."""
    from bi_agent.runtime.runtime_persistence import (
        _record_from_payload,
        validate_analysis_runtime_records,
    )
    from bi_agent.runtime.reuse_decision import (
        PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION,
        validated_physical_query_reuse_decision_record,
    )

    if not run_id or not thread_id or not topic_id:
        raise ValueError("runtime_evaluation_authority_owner_missing")
    if run_status != "completed":
        raise ValueError("runtime_evaluation_run_incomplete")
    if (
        not isinstance(delivery_verifier, Mapping)
        or delivery_verifier.get("status") != "passed"
        or not isinstance(delivery_verifier.get("errors"), (list, tuple))
        or delivery_verifier.get("errors")
    ):
        raise ValueError("runtime_evaluation_delivery_verifier_rejected")
    if (
        not publication_digest
        or canonical_digest(bundle) != publication_digest
    ):
        raise ValueError("runtime_evaluation_publication_digest_mismatch")

    contract = bundle.get("analysis_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("runtime_authority_contract_missing")
    contract_ref = str(contract.get("analysis_contract_id") or "")
    contract_signature = str(contract.get("contract_signature") or "")
    contract_payload = {
        key: value for key, value in contract.items() if key != "contract_signature"
    }
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("runtime_evaluation_analysis_contract_invalid") from exc
    if (
        not contract_ref
        or stored_contract_signature != contract_signature
        or analysis_contract_signature(typed_contract) != contract_signature
    ):
        raise ValueError("runtime_evaluation_analysis_contract_digest_mismatch")

    def records(key: str) -> list[dict[str, Any]]:
        raw = bundle.get(key)
        if not isinstance(raw, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in raw
        ):
            raise ValueError(f"runtime_evaluation_{key}_invalid")
        return [dict(item) for item in raw]

    def unique(items: list[dict[str, Any]], key: str, error: str) -> None:
        refs = [str(item.get(key) or "") for item in items]
        if any(not ref for ref in refs) or len(refs) != len(set(refs)):
            raise ValueError(error)

    raw_records = {
        key: records(key)
        for key in (
            "query_contracts",
            "query_execution_records",
            "rows_records",
            "snapshot_records",
            "completeness_records",
            "capability_binding_records",
            "evidence_manifests",
            "context_manifests",
            "trusted_provenance_records",
            "verified_claims",
            "claim_links",
            "repair_attempts",
        )
    }
    query_contracts = raw_records["query_contracts"]
    unique(
        query_contracts,
        "query_contract_id",
        "runtime_evaluation_query_contract_ambiguous",
    )
    try:
        typed_queries = tuple(_query_contract_from_mapping(item) for item in query_contracts)
        typed_groups = {
            key: tuple(_record_from_payload(kind, item) for item in raw_records[key])
            for key, kind in (
                ("query_execution_records", "query_execution"),
                ("rows_records", "rows"),
                ("snapshot_records", "snapshot"),
                ("completeness_records", "completeness"),
                ("capability_binding_records", "capability_binding"),
            )
        }
    except Exception as exc:
        raise ValueError("runtime_evaluation_authority_record_invalid") from exc

    if any(item.analysis_contract_ref != contract_ref for item in typed_queries):
        raise ValueError("runtime_evaluation_authority_cross_run")
    executions = typed_groups["query_execution_records"]
    bindings = typed_groups["capability_binding_records"]
    if any(
        item.contract.analysis_contract_ref != contract_ref for item in executions
    ) or any(item.analysis_contract_ref != contract_ref for item in bindings):
        raise ValueError("runtime_evaluation_authority_cross_run")
    available_results = {item.result_ref for item in executions}
    required_results = {
        ref
        for binding in bindings
        for ref in (*binding.result_refs, *binding.validation_result_refs)
    }
    if not required_results.issubset(available_results):
        raise ValueError("runtime_evaluation_query_execution_missing")

    try:
        validate_analysis_runtime_records(
            run_id=run_id,
            analysis_contract=contract,
            query_contracts=typed_queries,
            query_execution_records=executions,
            rows_records=typed_groups["rows_records"],
            snapshot_records=typed_groups["snapshot_records"],
            completeness_records=typed_groups["completeness_records"],
            capability_binding_records=bindings,
            evidence_manifests=raw_records["evidence_manifests"],
            context_manifests=raw_records["context_manifests"],
            trusted_provenance_records=raw_records["trusted_provenance_records"],
            verified_claims=raw_records["verified_claims"],
            claim_links=raw_records["claim_links"],
            repair_attempts=raw_records["repair_attempts"],
        )
    except (EvidenceIntegrityError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("runtime_evaluation_authority_invalid") from exc

    bindings_by_ref = {item.record_ref: item for item in bindings}
    evidence = []
    for item in raw_records["evidence_manifests"]:
        binding_ref = str(item.get("binding_record_ref") or "")
        binding = bindings_by_ref[binding_ref]
        evidence.append(
            {
                **item,
                "binding_manifest_ref": binding_ref,
                "binding_manifest_digest": binding.binding_digest,
            }
        )

    raw_reuse_decisions = [
        item
        for provenance in raw_records["trusted_provenance_records"]
        for item in provenance.get("reuse_decisions") or ()
        if isinstance(item, Mapping)
        and item.get("schema_version")
        == PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION
    ]
    try:
        reuse_decisions = [
            validated_physical_query_reuse_decision_record(item)
            for item in raw_reuse_decisions
        ]
    except (EvidenceIntegrityError, TypeError, ValueError) as exc:
        raise ValueError("runtime_evaluation_reuse_decision_invalid") from exc
    decision_refs = [item["decision_ref"] for item in reuse_decisions]
    if len(decision_refs) != len(set(decision_refs)):
        raise ValueError("runtime_evaluation_reuse_decision_ambiguous")
    queries_by_ref = {item.query_contract_id: item for item in typed_queries}
    executions_by_record = {item.record_ref: item for item in executions}
    completeness_by_ref = {
        item.record_ref: item for item in typed_groups["completeness_records"]
    }
    for decision in reuse_decisions:
        if (
            decision["run_id"] != run_id
            or decision["topic_id"] != topic_id
            or decision["analysis_contract_ref"] != contract_ref
        ):
            raise ValueError("runtime_evaluation_authority_cross_run")
        query = queries_by_ref.get(decision["query_contract_ref"])
        execution = executions_by_record.get(
            decision["query_execution_record_ref"]
        )
        if execution is None:
            raise ValueError("runtime_evaluation_query_execution_missing")
        if (
            query is None
            or query.contract_signature != decision["query_contract_signature"]
            or execution.query_contract_ref != decision["query_contract_ref"]
            or execution.result_ref != decision["result_ref"]
        ):
            raise ValueError("runtime_evaluation_reuse_decision_invalid")
        decision_completeness = [
            completeness_by_ref.get(ref)
            for ref in decision["completeness_record_refs"]
        ]
        if any(item is None for item in decision_completeness):
            raise ValueError("runtime_evaluation_completeness_missing")
        if any(
            item.result_ref != decision["result_ref"]
            or item.query_contract_ref != decision["query_contract_ref"]
            for item in decision_completeness
        ):
            raise ValueError("runtime_evaluation_reuse_decision_invalid")

    query_results = [canonical_value(item.result_payload) for item in executions]
    reports = [
        canonical_value(item.report_payload)
        for item in typed_groups["completeness_records"]
    ]
    plans_by_ref: dict[str, Mapping[str, Any]] = {}
    for binding in bindings:
        plan_ref = canonical_digest(binding.plan_payload)
        plans_by_ref.setdefault(plan_ref, canonical_value(binding.plan_payload))
    query_contracts = raw_records["query_contracts"]
    query_executions = raw_records["query_execution_records"]
    completeness_records = raw_records["completeness_records"]
    capability_bindings = raw_records["capability_binding_records"]
    context_manifests = raw_records["context_manifests"]
    provenance_records = raw_records["trusted_provenance_records"]
    verified_claims = raw_records["verified_claims"]
    claim_links = raw_records["claim_links"]
    projection = {
        "projection_schema_version": "eval-runtime-authority.v1",
        "run_id": run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "turn_id": turn_id,
        "run_status": run_status,
        "publication_digest": publication_digest,
        "analysis_contract": dict(contract),
        "stored_contract_signature": contract_signature,
        "query_contracts": query_contracts,
        "query_executions": query_executions,
        "completeness_records": completeness_records,
        "capability_bindings": capability_bindings,
        "evidence_manifests": evidence,
        "context_manifests": context_manifests,
        "trusted_provenance_records": provenance_records,
        "verified_claims": verified_claims,
        "claim_links": claim_links,
        "delivery_verifier": dict(delivery_verifier),
        "reuse_decisions": reuse_decisions,
        "sections": [
            {
                "section_id": "summary",
                "payload": {"claims": verified_claims},
            },
            {
                "section_id": "evidence",
                "payload": {"evidence": evidence},
            },
        ],
        "admin_audit": {
            "analysis_contract": contract_payload,
            "compiler_runtime_plan": {"analysis_contract": contract_payload},
            "query_contracts": query_contracts,
            "query_results": query_results,
            "query_executions": query_executions,
            "completeness_reports": reports,
            "completeness_records": completeness_records,
            "capability_execution_plans": list(plans_by_ref.values()),
            "capability_bindings": capability_bindings,
            "verifier": dict(delivery_verifier),
            "reuse_decisions": reuse_decisions,
        },
    }
    return canonical_value(projection)


def _runtime_evidence_resolver_for_store(
    conversation_store: Any,
    *,
    fallback: Any = None,
    required: bool = False,
):
    """Select persisted runtime evidence authority when the store provides it."""
    factory = getattr(conversation_store, "runtime_evidence_resolver", None)
    if not callable(factory):
        if required:
            raise RuntimeError("eval_runtime_evidence_authority_unavailable")
        return fallback
    try:
        resolver = factory()
    except Exception as exc:
        raise RuntimeError("eval_runtime_evidence_authority_unavailable") from exc
    if resolver is None:
        raise RuntimeError("eval_runtime_evidence_authority_unavailable")
    return resolver


def _present_analysis_contract_copies(
    *containers: Mapping[str, Any],
) -> tuple[Any, ...]:
    return tuple(
        container["analysis_contract"]
        for container in containers
        if "analysis_contract" in container
    )


def _analysis_contract_copy_issue(
    copies: tuple[Any, ...],
    authoritative_contract: Any,
) -> str:
    for raw_contract in copies:
        if (
            not isinstance(raw_contract, Mapping)
            or not isinstance(raw_contract.get("analysis_contract_id"), str)
            or not raw_contract.get("analysis_contract_id")
        ):
            return "invalid"
        try:
            typed_contract = analysis_contract_from_dict(raw_contract)
        except (KeyError, TypeError, ValueError):
            return "invalid"
        if (
            typed_contract.analysis_contract_id
            != authoritative_contract.analysis_contract_id
        ):
            return "id_mismatch"
        if canonical_value(typed_contract.to_dict()) != canonical_value(
            authoritative_contract.to_dict()
        ):
            return "content_mismatch"
    return ""


def _runtime_audit_package(
    result: Mapping[str, Any],
    *,
    authority_resolver: Any = None,
) -> dict[str, Any]:
    def failure(reason: str) -> dict[str, Any]:
        return {"_authority_error": reason}

    raw_path = result.get("artifact_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return failure("artifact_path_missing")
    raw = Path(raw_path)
    if ".." in raw.parts:
        return failure("artifact_path_outside_root")
    artifact_root = (ROOT / "artifacts").resolve()
    candidate = raw if raw.is_absolute() else ROOT / raw
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(artifact_root)
    except FileNotFoundError:
        return failure("artifact_missing")
    except (OSError, RuntimeError, ValueError):
        return failure("artifact_path_outside_root")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return failure("artifact_invalid")
    if not isinstance(payload, Mapping):
        return failure("artifact_invalid")
    raw_expected_run_id = result.get("run_id")
    raw_payload_run_id = payload.get("run_id")
    if raw_expected_run_id is None or raw_expected_run_id == "":
        return failure("run_id_missing")
    if not isinstance(raw_expected_run_id, str):
        return failure("run_id_invalid")
    if raw_payload_run_id is None or raw_payload_run_id == "":
        return failure("persisted_run_id_missing")
    if not isinstance(raw_payload_run_id, str):
        return failure("persisted_run_id_invalid")
    expected_run_id = raw_expected_run_id
    payload_run_id = raw_payload_run_id
    if payload_run_id != expected_run_id:
        return failure("run_id_mismatch")
    if not callable(authority_resolver):
        return failure("missing_runtime_authority_resolver")
    try:
        resolved_authority = authority_resolver(expected_run_id)
    except Exception:
        return failure("runtime_authority_resolution_failed")
    if not isinstance(resolved_authority, Mapping):
        return failure("persisted_analysis_contract_missing")
    resolved_run_id = resolved_authority.get("run_id")
    if not isinstance(resolved_run_id, str) or not resolved_run_id:
        return failure("runtime_authority_run_id_invalid")
    if resolved_run_id != expected_run_id:
        return failure("runtime_authority_run_id_mismatch")
    raw_authority_contract = resolved_authority.get("analysis_contract")
    if not isinstance(raw_authority_contract, Mapping):
        return failure("persisted_analysis_contract_missing")
    contract_payload = dict(raw_authority_contract)
    embedded_signature = str(
        contract_payload.pop("contract_signature", "") or ""
    )
    stored_signature = str(
        resolved_authority.get("stored_contract_signature") or ""
    )
    try:
        persisted_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError):
        return failure("persisted_analysis_contract_invalid")
    if (
        not embedded_signature
        or not stored_signature
        or embedded_signature != stored_signature
        or analysis_contract_signature(persisted_contract) != stored_signature
    ):
        return failure("persisted_analysis_contract_signature_mismatch")
    if not persisted_contract.analysis_contract_id:
        return failure("analysis_contract_id_mismatch")

    raw_admin = payload.get("admin_audit")
    if "admin_audit" in payload and not isinstance(raw_admin, Mapping):
        return failure("persisted_analysis_contract_invalid")
    admin = raw_admin if isinstance(raw_admin, Mapping) else {}
    artifact_copies = _present_analysis_contract_copies(payload, admin)
    artifact_persistence_present = "analysis_runtime_persistence" in admin
    if not artifact_copies and not artifact_persistence_present:
        return failure("persisted_analysis_contract_missing")
    if artifact_persistence_present:
        artifact_persistence = admin.get("analysis_runtime_persistence")
        if not isinstance(artifact_persistence, Mapping):
            return failure("persisted_analysis_contract_ref_invalid")
        if (
            artifact_persistence.get("status") != "persisted"
            or artifact_persistence.get("analysis_contract_ref")
            != persisted_contract.analysis_contract_id
        ):
            return failure("persisted_analysis_contract_ref_mismatch")
    artifact_copy_issue = _analysis_contract_copy_issue(
        artifact_copies,
        persisted_contract,
    )
    if artifact_copy_issue:
        if artifact_copy_issue == "invalid":
            return failure("persisted_analysis_contract_invalid")
        return failure("persisted_analysis_contract_mismatch")

    client_package = result.get("answer_package") or {}
    if not isinstance(client_package, Mapping):
        client_package = {}
    raw_client_admin = client_package.get("admin_audit")
    if "admin_audit" in client_package and not isinstance(
        raw_client_admin,
        Mapping,
    ):
        return failure("effective_analysis_contract_invalid")
    client_admin = (
        raw_client_admin if isinstance(raw_client_admin, Mapping) else {}
    )
    if "analysis_runtime_persistence" in client_admin:
        client_persistence = client_admin.get("analysis_runtime_persistence")
        if not isinstance(client_persistence, Mapping):
            return failure("effective_analysis_contract_ref_invalid")
        if (
            client_persistence.get("status") != "persisted"
            or client_persistence.get("analysis_contract_ref")
            != persisted_contract.analysis_contract_id
        ):
            return failure("effective_analysis_contract_ref_mismatch")
    client_copy_issue = _analysis_contract_copy_issue(
        _present_analysis_contract_copies(
            result,
            client_package,
            client_admin,
        ),
        persisted_contract,
    )
    if client_copy_issue == "invalid":
        return failure("effective_analysis_contract_invalid")
    if client_copy_issue == "id_mismatch":
        return failure("effective_analysis_contract_id_mismatch")
    if client_copy_issue == "content_mismatch":
        return failure("effective_analysis_contract_mismatch")
    if (
        resolved_authority.get("projection_schema_version")
        == "eval-runtime-authority.v1"
    ):
        projection_admin = resolved_authority.get("admin_audit")
        if not isinstance(projection_admin, Mapping):
            return failure("runtime_evaluation_projection_invalid")
        return {
            **dict(payload),
            **dict(resolved_authority),
            "admin_audit": dict(projection_admin),
        }
    return {
        **dict(payload),
        "admin_audit": {
            **dict(admin),
            "analysis_contract": persisted_contract.to_dict(),
        },
    }


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
    evaluated_turns = _evaluated_turns(turns)
    if not evaluated_turns:
        return {
            "required": bool(real_clickhouse),
            "real_clickhouse_verified": None,
            "clickhouse_result_refs": [],
            "observed_datasets": [],
            "required_datasets": list(required_datasets),
            "runtime_correctness": {
                key: None for key in _RUNTIME_CORRECTNESS_KEYS
            },
            "issues": [],
        }
    refs: list[str] = []
    datasets: set[str] = set()
    issues: list[str] = []
    verified = True
    runtime_correctness = {
        key: True
        for key in _RUNTIME_CORRECTNESS_KEYS
    }
    for turn in evaluated_turns:
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


def _runtime_authority_failure_reason(
    turn: Mapping[str, Any],
) -> str:
    if str(_effective_result(dict(turn)).get("status") or "") != "completed":
        return ""
    if "runtime_authority" not in turn:
        return "runtime_authority_not_evaluated"
    authority = turn.get("runtime_authority")
    if not isinstance(authority, Mapping):
        return "runtime_authority_invalid"
    return str(authority.get("_authority_error") or "")


def _expectation_review_passed(turn: Mapping[str, Any]) -> bool:
    review = turn.get("expectation_review")
    return isinstance(review, Mapping) and review.get("passed") is True


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
    evaluated_turns = _evaluated_turns(turns)
    failure_evaluations = tuple(
        evaluation
        for turn in turns
        for evaluation in (_turn_failure_evaluation(turn),)
        if evaluation is not None
    )
    primary_failures = [
        dict(evaluation["primary_failure"])
        for evaluation in failure_evaluations
    ]
    expectation_failed = any(
        not _expectation_review_passed(turn) for turn in evaluated_turns
    )
    runtime_authority_errors = sorted(
        {
            reason
            for turn in evaluated_turns
            for reason in (_runtime_authority_failure_reason(turn),)
            if reason
        }
    )
    runtime_authority_failed = bool(runtime_authority_errors)
    obligation_failed = any(
        (turn.get("obligation_review") or {}).get("hard_acceptance_passed") is False
        for turn in evaluated_turns
    )
    strict_quality_failed = (
        any(turn.get("strict_quality_failed") is True for turn in evaluated_turns)
        if evaluated_turns
        else None
    )
    real_clickhouse_review = _aggregate_real_clickhouse_review(
        turns,
        real_clickhouse,
        case.get("required_datasets") or (),
    )
    real_clickhouse_failed = (
        real_clickhouse_review["real_clickhouse_verified"] is False
    )
    quality_warnings = sorted(
        {
            str(warning)
            for turn in evaluated_turns
            for warning in (
                _effective_quality_review(turn).get("quality_warnings") or ()
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
            if (
                failure_evaluations
                or expectation_failed
                or runtime_authority_failed
                or obligation_failed
                or strict_quality_failed is True
                or real_clickhouse_failed
            )
            else "passed"
        ),
        "strict_quality": strict_quality,
        "strict_quality_failed": strict_quality_failed,
        "quality_warnings": quality_warnings,
        "quality_warning_count": len(quality_warnings),
        "real_clickhouse_review": real_clickhouse_review,
        "real_clickhouse_verified": real_clickhouse_review["real_clickhouse_verified"],
        "clickhouse_result_refs": real_clickhouse_review["clickhouse_result_refs"],
        "primary_failure": primary_failures[0] if primary_failures else None,
        "primary_failures": primary_failures,
        "failure_reason": (
            primary_failures[0]["reason"] if primary_failures else None
        ),
        "runtime_authority_failed": runtime_authority_failed,
        "runtime_authority_errors": runtime_authority_errors,
        "final_turn_status": final_result.get("status"),
        "run_id": final_result.get("run_id"),
        "topic_id": final_result.get("topic_id"),
        "answer_package": final_result.get("answer_package"),
        "context_manifest": final_result.get("context_manifest"),
        "accepted_graph": final_result.get("accepted_graph") or [],
        "llm_calls": final_result.get("llm_calls", []),
        "quality_review": final_result.get("quality_review"),
        "coverage_summary": _coverage_summary(turns),
        "turns": turns,
    }


def _coverage_summary(turns: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated_turns = _evaluated_turns(turns)
    obligations = [
        turn.get("obligation_review") or {}
        for turn in evaluated_turns
        if turn.get("obligation_review")
    ]
    required = sum(len(item.get("required_capabilities") or ()) for item in obligations)
    authored_families: dict[str, int] = {}
    persisted_families: dict[str, int] = {}
    persisted_family_sets: dict[str, int] = {}
    family_authority_statuses: dict[str, int] = {}
    for item in obligations:
        authored_family = str(item.get("authored_question_family") or "")
        if authored_family:
            authored_families[authored_family] = authored_families.get(authored_family, 0) + 1
        persisted_set = tuple(
            str(family)
            for family in (
                item.get("question_families")
                or ((item.get("question_family"),) if item.get("question_family") else ())
            )
            if str(family)
        )
        for persisted_family in persisted_set:
            persisted_families[persisted_family] = (
                persisted_families.get(persisted_family, 0) + 1
            )
        if persisted_set:
            set_key = "|".join(persisted_set)
            persisted_family_sets[set_key] = persisted_family_sets.get(set_key, 0) + 1
        authority_status = str(item.get("question_family_authority_status") or "")
        if authority_status:
            family_authority_statuses[authority_status] = (
                family_authority_statuses.get(authority_status, 0) + 1
            )
    expected_datasets: dict[str, dict[str, int]] = {}
    observed_datasets: dict[str, dict[str, int]] = {}
    for item in obligations:
        for dataset_id, state in (item.get("expected_dataset_states") or {}).items():
            states = expected_datasets.setdefault(str(dataset_id), {})
            states[str(state)] = states.get(str(state), 0) + 1
            observed_state = str(
                (item.get("observed_dataset_states") or {}).get(dataset_id)
                or "unobserved"
            )
            observed_states = observed_datasets.setdefault(str(dataset_id), {})
            observed_states[observed_state] = observed_states.get(observed_state, 0) + 1
        for dataset_id, state in (item.get("observed_dataset_states") or {}).items():
            if dataset_id in (item.get("expected_dataset_states") or {}):
                continue
            states = observed_datasets.setdefault(str(dataset_id), {})
            states[str(state)] = states.get(str(state), 0) + 1
    outcome_counts = {
        outcome: sum(
            1
            for item in obligations
            for value in (item.get("capability_outcomes") or {}).values()
            if value == outcome
        )
        for outcome in (
            "executed",
            "degraded",
            "blocked",
            "unobserved",
            "missing_route",
        )
    }
    runtime_correctness = {
        key: (
            all(
                (
                    (turn.get("real_clickhouse_review") or {}).get(
                        "runtime_correctness"
                    )
                    or {}
                ).get(key)
                is True
                for turn in evaluated_turns
            )
            if evaluated_turns
            else None
        )
        for key in _RUNTIME_CORRECTNESS_KEYS
    }
    runtime_passed = (
        all(runtime_correctness.values()) if evaluated_turns else None
    )
    obligation_passed = (
        bool(obligations) and all(
            item.get("hard_acceptance_passed") is True for item in obligations
        )
        if evaluated_turns
        else None
    )
    hard_acceptance_passed = (
        runtime_passed and obligation_passed
        if runtime_passed is not None and obligation_passed is not None
        else None
    )
    return {
        "question_family_coverage": {
            "authored": authored_families,
            "persisted": persisted_families,
            "persisted_sets": persisted_family_sets,
            "authority_status": family_authority_statuses,
            "mismatches": family_authority_statuses.get("mismatch", 0),
        },
        "obligation_coverage": {
            "required": required,
            "routed": required - outcome_counts["missing_route"],
            "terminal": (
                outcome_counts["executed"]
                + outcome_counts["degraded"]
                + outcome_counts["blocked"]
            ),
            **outcome_counts,
        },
        "expected_dataset_coverage": expected_datasets,
        "observed_dataset_coverage": observed_datasets,
        "dataset_coverage": {
            "deprecated": True,
            "meaning": "expected_dataset_coverage",
            "coverage": expected_datasets,
        },
        "dataset_coverage_deprecated": {
            "meaning": "expected_dataset_coverage",
            "coverage": expected_datasets,
        },
        "runtime_correctness": runtime_correctness,
        "hard_acceptance": {
            "runtime_passed": runtime_passed,
            "obligation_passed": obligation_passed,
            "passed": hard_acceptance_passed,
        },
        "answer_quality": {
            "blocking": False,
            "warning_count": sum(
                len(_effective_quality_review(turn).get("quality_warnings") or ())
                for turn in evaluated_turns
            ),
        },
        "final_answer_audit_coverage": {
            "reviewed": sum(
                _has_completed_final_answer_audit(turn)
                for turn in evaluated_turns
            ),
            "total": len(evaluated_turns),
        },
        "clarification_resume": {
            "required": sum(
                (turn.get("scenario") or {}).get("clarification_resume") == "required"
                for turn in evaluated_turns
            ),
            "passed": sum(
                (turn.get("scenario") or {}).get("clarification_resume") == "required"
                and (turn.get("obligation_review") or {}).get("hard_acceptance_passed") is True
                and (turn.get("obligation_review") or {}).get("clarification_resume_passed") is True
                and turn.get("resumed_status") == "completed"
                and turn.get("resumed_topic_id") == turn.get("topic_id")
                for turn in evaluated_turns
            ),
        },
        "reuse_coverage": {
            "required": sum(
                (turn.get("scenario") or {}).get("reuse") == "required"
                for turn in evaluated_turns
            ),
            "passed": sum(
                (turn.get("scenario") or {}).get("reuse") == "required"
                and (turn.get("obligation_review") or {}).get("hard_acceptance_passed") is True
                and (turn.get("obligation_review") or {}).get("reuse_passed") is True
                and bool(turn.get("prior_topic_id"))
                and turn.get("topic_id") == turn.get("prior_topic_id")
                for turn in evaluated_turns
            ),
        },
    }


def _has_completed_final_answer_audit(
    turn: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
) -> bool:
    effective = _effective_result(dict(turn))
    if str(effective.get("status") or "") != "completed":
        return False
    internal_package = _load_run_matched_internal_answer_package(
        effective,
        artifact_root=(
            artifact_root
            if artifact_root is not None
            else ROOT / "artifacts" / "phase-7"
        ),
    )
    if internal_package is None:
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("task") == "final_answer_audit"
        and isinstance(item.get("structured_output"), Mapping)
        for item in internal_package.get("llm_calls") or ()
    )


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
    views = {
        "raw": output,
        "runtime-review": {
            "case_id": case_id,
            "runtime_correctness": output.get("coverage_summary", {}).get("runtime_correctness", {}),
            "hard_acceptance": deepcopy(
                output.get("coverage_summary", {}).get("hard_acceptance", {})
            ),
            "turns": [
                {
                    "index": turn.get("index"),
                    "clickhouse_runtime": turn.get("real_clickhouse_review"),
                    "obligation_review": turn.get("obligation_review"),
                }
                for turn in output.get("turns", [])
            ],
        },
        "quality-review": {
            "case_id": case_id,
            "answer_quality": output.get("coverage_summary", {}).get("answer_quality", {}),
            "turns": [
                _effective_quality_review(turn)
                for turn in output.get("turns", [])
            ],
        },
        "coverage-summary": output.get("coverage_summary", {}),
    }
    for suffix, payload in views.items():
        (artifact_dir / f"{case_id}.{suffix}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
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
    core_artifact_root = (ROOT / "artifacts" / "phase-7").resolve()
    analysis_context = dict(case.get("analysis_context") or {})
    coverage_authority = None
    coverage_authority_initialized = False
    requires_coverage_authority = any(
        isinstance(turn.get("scenario"), Mapping)
        and bool((turn.get("scenario") or {}).get("expected_dataset_states"))
        for turn in case.get("turns") or ()
        if isinstance(turn, Mapping)
    )
    required_datasets = tuple(case.get("required_datasets") or ())
    runtime_authority_resolver = None
    runtime_evidence_resolver = None
    runtime_resolvers_initialized = False
    turns: list[dict[str, Any]] = []
    prior_run_lineage: list[dict[str, str]] = []
    prior_topic_id: str | None = None
    for index, turn in enumerate(case["turns"], start=1):
        result = core.run_message(
            thread_id=thread_id,
            user_message=turn["user"],
            artifact_root=str(core_artifact_root),
            analysis_context=analysis_context or None,
        )
        turn_record = {
            "index": index,
            "thread_id": thread_id,
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
            "quality_review": _runtime_quality_review(
                result,
                artifact_root=core_artifact_root,
            ),
            "clarification": result.get("clarification"),
            "artifact_path": result.get("artifact_path"),
            "scenario": dict(turn.get("scenario") or {}),
            "prior_topic_id": prior_topic_id,
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
                artifact_root=str(core_artifact_root),
                analysis_context=analysis_context or None,
            )
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
            turn_record["resumed_quality_review"] = _runtime_quality_review(
                resumed,
                artifact_root=core_artifact_root,
            )
            turn_record["resumed_clarification"] = resumed.get("clarification")
            turn_record["resumed_artifact_path"] = resumed.get("artifact_path")
            current = resumed
        if clarification_resumes:
            turn_record["clarification_resumes"] = clarification_resumes
        effective = _effective_result(turn_record)
        failure_evaluation = _run_failure_evaluation(turn_record)
        if failure_evaluation is not None:
            turn_record["evaluation"] = failure_evaluation
            turn_record["expectation_review"] = {
                **failure_evaluation,
                "passed": None,
            }
            turn_record["obligation_review"] = {
                **failure_evaluation,
                "hard_acceptance_passed": None,
            }
            turn_record["real_clickhouse_review"] = _failed_run_clickhouse_review(
                failure_evaluation,
                real_clickhouse=real_clickhouse,
                required_datasets=required_datasets,
            )
            turn_record["strict_quality_failed"] = None
        else:
            if (
                real_clickhouse
                and requires_coverage_authority
                and not coverage_authority_initialized
            ):
                raw_as_of = analysis_context.get("as_of")
                if not isinstance(raw_as_of, str):
                    raise RuntimeError("eval_coverage_authority_as_of_required")
                try:
                    coverage_authority = audit_existing_data_coverage(
                        RuntimeContractRegistry.from_path(
                            CANONICAL_RUNTIME_BINDINGS_PATH
                        ),
                        snapshot_records=core.store.list_dataset_snapshots(),
                        release_resolver=core.release_resolver,
                        as_of=datetime.fromisoformat(raw_as_of),
                        permission_scope="analyst",
                    )
                except Exception as exc:
                    raise RuntimeError("eval_coverage_authority_unavailable") from exc
                coverage_authority_initialized = True
            if not runtime_resolvers_initialized:
                runtime_authority_resolver = _runtime_authority_resolver_for_store(
                    getattr(core, "store", None)
                )
                runtime_evidence_resolver = _runtime_evidence_resolver_for_store(
                    getattr(core, "store", None),
                    fallback=getattr(core, "evidence_resolver", None),
                    required=real_clickhouse,
                )
                runtime_resolvers_initialized = True
            turn_record["expectation_review"] = _review_expectations(
                turn,
                turn_record,
            )
            requires_runtime_authority = (
                str(effective.get("status") or "") == "completed"
                or bool(turn_record["scenario"])
            )
            runtime_authority = (
                _runtime_audit_package(
                    effective,
                    authority_resolver=runtime_authority_resolver,
                )
                if requires_runtime_authority
                else None
            )
            turn_record["real_clickhouse_review"] = _real_clickhouse_review(
                effective,
                real_clickhouse=real_clickhouse,
                evidence_resolver=runtime_evidence_resolver,
                required_datasets=required_datasets,
                analysis_context=analysis_context,
                runtime_authority_resolver=runtime_authority_resolver,
                runtime_authority=runtime_authority,
            )
            if requires_runtime_authority:
                turn_record["runtime_authority"] = runtime_authority
            if turn_record["scenario"]:
                turn_record["obligation_review"] = review_case_obligations(
                    turn_record,
                    RuntimeContractRegistry.from_path(
                        CANONICAL_RUNTIME_BINDINGS_PATH
                    ),
                    coverage_authority=coverage_authority,
                    evidence_resolver=runtime_evidence_resolver,
                    rows_loader=getattr(core, "rows_loader", None),
                    release_resolver=getattr(core, "release_resolver", None),
                    conversation_store=getattr(core, "store", None),
                    case_lineage={
                        "thread_id": thread_id,
                        "current_run_id": str(effective.get("run_id") or ""),
                        "current_topic_id": str(effective.get("topic_id") or ""),
                        "prior_runs": list(prior_run_lineage),
                    },
                )
            turn_record["strict_quality_failed"] = bool(
                strict_quality and _strict_quality_failed(turn_record)
            )
        turns.append(turn_record)
        prior_run_lineage.append({
            "run_id": str(effective.get("run_id") or ""),
            "thread_id": thread_id,
            "topic_id": str(effective.get("topic_id") or ""),
            "status": str(effective.get("status") or ""),
        })
        prior_topic_id = _effective_result(turn_record).get("topic_id")
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        choices=("fixed-eight", "platform-current-data"),
        default=None,
    )
    parser.add_argument("--cases")
    parser.add_argument("--case")
    parser.add_argument("--artifact-dir")
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--real-clickhouse", action="store_true")
    parser.add_argument("--strict-quality", action="store_true")
    args = parser.parse_args(argv)

    load_env_file()
    run_mode = _run_mode(real_llm=args.real_llm, real_clickhouse=args.real_clickhouse)
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else _default_artifact_dir(
        real_llm=args.real_llm,
        real_clickhouse=args.real_clickhouse,
    )
    try:
        selected = resolve_cli_cases(args.suite, args.cases, args.case)
    except (OSError, ValueError) as exc:
        error_code = str(exc).split(":", 1)[0]
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": error_code,
                    "owner": "eval_operator",
                    "impact": "no evaluation cases were executed",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
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
