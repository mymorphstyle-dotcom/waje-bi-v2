from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.runtime.analysis_obligations import (
    ObligationRequest,
    resolve_analysis_obligations,
)
from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict
from bi_agent.runtime.claim_provenance import (
    validate_trusted_claim_provenance_record,
    validate_verified_claim_record,
)
from bi_agent.runtime.coverage_audit import audit_existing_data_coverage
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_value
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


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


def review_case_obligations(
    turn_record: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    *,
    coverage_authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Review executable obligations without constraining answer wording."""
    scenario = turn_record.get("scenario") or {}
    if not isinstance(scenario, Mapping):
        raise ValueError("scenario_expectation_invalid")
    family = str(scenario.get("question_family") or "")
    request = ObligationRequest(
        question_families=(family,) if family else (),
        diagnostic_tags=tuple(scenario.get("diagnostic_tags") or ()),
        target_metrics=tuple(scenario.get("target_metrics") or ()),
        requested_dimensions=tuple(scenario.get("requested_dimensions") or ()),
        baselines=tuple(scenario.get("baselines") or ()),
        context_sources=tuple(scenario.get("context_sources") or ()),
        claim_intents=tuple(scenario.get("claim_intents") or ()),
    )
    resolution = resolve_analysis_obligations(request, registry)
    required = tuple(
        dict.fromkeys(
            (*resolution.required_capabilities,
             *resolution.conditional_capabilities,
             *resolution.independent_capabilities,
             *(str(item) for item in scenario.get("required_capabilities") or ()))
        )
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
                question_family=family,
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
            question_family=family,
            coverage_authority=coverage_authority,
        )
    authority = turn_record.get("runtime_authority") or {}
    if not isinstance(authority, Mapping):
        raise ValueError("runtime_authority_invalid")
    observed_states, observed_gaps = _derive_runtime_dataset_states(authority)
    capability_outcomes = _derive_capability_outcomes(
        required,
        accepted_capabilities=actual,
        authority=authority,
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
        if observed_states.get(dataset_id) != state
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
    claim_review = _review_claim_ceiling(authority, scenario, registry)
    resolved_terminal_boundary = _authority_resolved_terminal_boundary(
        str(scenario.get("terminal_boundary") or ""), expected_states
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
    reuse_passed = (
        not reuse_required
        or bool(turn_record.get("prior_topic_id"))
        and turn_record.get("topic_id") == turn_record.get("prior_topic_id")
        and _has_exact_reuse_decision(authority)
    )
    hard_passed = (
        not nonterminal_capabilities
        and not capability_state_mismatches
        and not unresolved_authority_capabilities
        and not unresolved_authority_roles
        and not missing_data
        and not mismatched_gaps
        and not missing_expected_gaps
        and claim_review["passed"]
        and terminal_review["passed"]
        and clarification_passed
        and reuse_passed
    )
    return {
        "question_family": family,
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
        "reuse_passed": reuse_passed,
        "hard_acceptance_passed": hard_passed,
    }


def _authority_resolved_capability_states(
    authored_states: Mapping[str, Any],
    *,
    dataset_roles: tuple[str, ...],
    question_family: str,
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
                not families or question_family in families
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
    question_family: str,
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
            if capability in required_set:
                required_states.append(str(cell.get("state") or ""))
            if question_family in families:
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


def _derive_capability_outcomes(
    required: tuple[str, ...],
    *,
    accepted_capabilities: set[str],
    authority: Mapping[str, Any],
) -> dict[str, str]:
    executions = _mapping_items_for_keys(
        authority,
        {"query_executions", "query_results"},
    )
    result_readiness: dict[str, str] = {}
    for execution in executions:
        refs = tuple(
            dict.fromkeys(
                str(ref)
                for ref in (
                    execution.get("result_ref"),
                    *(execution.get("result_refs") or ()),
                )
                if ref
            )
        )
        execution_status = str(
            execution.get("execution_status") or execution.get("status") or ""
        )
        completeness = str(execution.get("completeness_status") or "")
        readiness = str(execution.get("analysis_readiness") or "")
        outcome = ""
        if (
            execution_status in {"succeeded", "completed", "executed", "ready"}
            and completeness in {"complete", "ready"}
            and readiness in {"", "ready"}
        ):
            outcome = "executed"
        elif (
            execution_status in {"succeeded", "completed", "executed", "degraded"}
            and completeness == "partial"
            and readiness == "degraded"
        ):
            outcome = "degraded"
        for ref in refs:
            if outcome == "executed" or ref not in result_readiness:
                result_readiness[ref] = outcome

    bindings_by_capability: dict[str, list[Mapping[str, Any]]] = {}
    for binding in _mapping_items_for_keys(authority, {"capability_bindings"}):
        capability_id = str(binding.get("capability_id") or "")
        if capability_id:
            bindings_by_capability.setdefault(capability_id, []).append(binding)

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
        observed: set[str] = set()
        for binding in bindings_by_capability.get(capability_id, ()):
            binding_status = str(binding.get("status") or "")
            for ref in binding.get("result_refs") or ():
                readiness = result_readiness.get(str(ref), "")
                if binding_status == "ready" and readiness == "executed":
                    observed.add("executed")
                elif binding_status in {"ready", "degraded"} and readiness == "degraded":
                    observed.add("degraded")
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
    for item in _mapping_items_for_keys(
        authority,
        {"contract_gaps", "typed_gaps", "gaps", "errors"},
    ):
        gap_type = str(item.get("gap_type") or item.get("error_code") or "")
        normalized = _normalized_gap_state(gap_type)
        if not normalized:
            continue
        for dataset_id in _dataset_ids(item):
            gaps.setdefault(dataset_id, []).append(gap_type)
            _set_dataset_state(states, dataset_id, normalized)
    return states, {
        dataset_id: tuple(dict.fromkeys(items)) for dataset_id, items in gaps.items()
    }


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
) -> dict[str, Any]:
    claims = _mapping_items_for_keys(authority, {"verified_claims"})
    bindings = _mapping_items_for_keys(authority, {"capability_bindings"})
    evidence = _mapping_items_for_keys(authority, {"evidence_manifests", "evidence"})
    provenance = _mapping_items_for_keys(
        authority, {"trusted_provenance_records", "claim_provenance_records"}
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
        support_evidence = {
            str(ref) for ref in claim.get("evidence_refs") or () if ref
        }
        support_results = {
            str(ref) for ref in claim.get("result_refs") or () if ref
        }
        provenance_record = provenance_by_ref.get(
            str(claim.get("provenance_record_ref") or "")
        )
        if provenance_record:
            support_evidence.update(
                str(ref) for ref in provenance_record.get("evidence_refs") or () if ref
            )
            support_results.update(
                str(ref) for ref in provenance_record.get("result_refs") or () if ref
            )
        related: dict[str, Mapping[str, Any]] = {}
        for evidence_ref in support_evidence:
            manifest = evidence_by_ref.get(evidence_ref)
            if not manifest:
                continue
            binding_ref = str(manifest.get("binding_manifest_ref") or "")
            if binding_ref in binding_by_ref:
                related[binding_ref] = binding_by_ref[binding_ref]
            support_results.update(
                str(ref) for ref in manifest.get("result_refs") or () if ref
            )
        for binding_ref, binding in binding_by_ref.items():
            binding_results = {
                str(ref) for ref in binding.get("result_refs") or () if ref
            }
            if support_results & binding_results:
                related[binding_ref] = binding
        ceilings = tuple(
            str(binding.get("maximum_claim_strength") or "")
            for binding in related.values()
            if str(binding.get("maximum_claim_strength") or "")
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
                        str(binding.get("capability_id") or "")
                        for binding in related.values()
                        if binding.get("capability_id")
                    }
                ),
                "authority_ceiling": ceiling,
                "passed": passed,
                "error_code": "" if passed else "claim_strength_exceeds_authority",
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


def _has_exact_reuse_decision(authority: Mapping[str, Any]) -> bool:
    return any(
        item.get("decision") == "reuse"
        for item in _mapping_items_for_keys(authority, {"reuse_decisions"})
    )


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
) -> dict[str, Any]:
    package = _runtime_audit_package(result)
    if not package:
        inline_package = result.get("answer_package") or {}
        if isinstance(inline_package, Mapping):
            package = dict(inline_package)
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
        return {}
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    expected_run_id = str(result.get("run_id") or client_package.get("run_id") or "")
    payload_run_id = str(payload.get("run_id") or "")
    if not expected_run_id or not payload_run_id or payload_run_id != expected_run_id:
        return {}
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
    obligation_failed = any(
        (turn.get("obligation_review") or {}).get("hard_acceptance_passed") is False
        for turn in turns
    )
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
            if expectation_failed or obligation_failed or strict_quality_failed or real_clickhouse_failed
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
        "coverage_summary": _coverage_summary(turns),
        "turns": turns,
    }


def _coverage_summary(turns: list[dict[str, Any]]) -> dict[str, Any]:
    obligations = [
        turn.get("obligation_review") or {}
        for turn in turns
        if turn.get("obligation_review")
    ]
    required = sum(len(item.get("required_capabilities") or ()) for item in obligations)
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
        key: bool(turns) and all(
            ((turn.get("real_clickhouse_review") or {}).get("runtime_correctness") or {}).get(key)
            is True
            for turn in turns
        )
        for key in (
            "all_required_queries_complete",
            "all_capabilities_bound",
            "all_claims_traceable",
        )
    }
    obligation_passed = bool(obligations) and all(
        item.get("hard_acceptance_passed") is True for item in obligations
    )
    return {
        "obligation_coverage": {
            "required": required,
            "routed": (
                outcome_counts["executed"]
                + outcome_counts["degraded"]
                + outcome_counts["unobserved"]
            ),
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
            "runtime_passed": all(runtime_correctness.values()),
            "obligation_passed": obligation_passed,
            "passed": all(runtime_correctness.values()) and obligation_passed,
        },
        "answer_quality": {
            "blocking": False,
            "warning_count": sum(
                len((turn.get("quality_review") or {}).get("quality_warnings") or ())
                for turn in turns
            ),
        },
        "final_answer_audit_coverage": {
            "reviewed": sum(
                _has_completed_final_answer_audit(turn)
                for turn in turns
            ),
            "total": len(turns),
        },
        "clarification_resume": {
            "required": sum(
                (turn.get("scenario") or {}).get("clarification_resume") == "required"
                for turn in turns
            ),
            "passed": sum(
                (turn.get("scenario") or {}).get("clarification_resume") == "required"
                and (turn.get("obligation_review") or {}).get("hard_acceptance_passed") is True
                and (turn.get("obligation_review") or {}).get("clarification_resume_passed") is True
                and turn.get("resumed_status") == "completed"
                and turn.get("resumed_topic_id") == turn.get("topic_id")
                for turn in turns
            ),
        },
        "reuse_coverage": {
            "required": sum(
                (turn.get("scenario") or {}).get("reuse") == "required"
                for turn in turns
            ),
            "passed": sum(
                (turn.get("scenario") or {}).get("reuse") == "required"
                and (turn.get("obligation_review") or {}).get("hard_acceptance_passed") is True
                and (turn.get("obligation_review") or {}).get("reuse_passed") is True
                and bool(turn.get("prior_topic_id"))
                and turn.get("topic_id") == turn.get("prior_topic_id")
                and _has_exact_reuse_decision(turn.get("runtime_authority") or {})
                for turn in turns
            ),
        },
    }


def _has_completed_final_answer_audit(turn: Mapping[str, Any]) -> bool:
    effective = _effective_result(dict(turn))
    if str(effective.get("status") or "") != "completed":
        return False
    raw_path = effective.get("artifact_path")
    expected_run_id = str(effective.get("run_id") or "")
    if not isinstance(raw_path, str) or not raw_path.strip() or not expected_run_id:
        return False
    artifact_root = Path("artifacts").resolve()
    try:
        internal_path = Path(raw_path).resolve()
        internal_path.relative_to(artifact_root)
        internal_package = json.loads(internal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if str(internal_package.get("run_id") or "") != expected_run_id:
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
            "turns": [turn.get("quality_review") for turn in output.get("turns", [])],
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
    analysis_context = dict(case.get("analysis_context") or {})
    coverage_authority = None
    requires_coverage_authority = any(
        isinstance(turn.get("scenario"), Mapping)
        and bool((turn.get("scenario") or {}).get("expected_dataset_states"))
        for turn in case.get("turns") or ()
        if isinstance(turn, Mapping)
    )
    if real_clickhouse and requires_coverage_authority:
        raw_as_of = analysis_context.get("as_of")
        if not isinstance(raw_as_of, str):
            raise RuntimeError("eval_coverage_authority_as_of_required")
        try:
            coverage_authority = audit_existing_data_coverage(
                RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH),
                snapshot_records=core.store.list_dataset_snapshots(),
                release_resolver=core.release_resolver,
                as_of=datetime.fromisoformat(raw_as_of),
                permission_scope="analyst",
            )
        except Exception as exc:
            raise RuntimeError("eval_coverage_authority_unavailable") from exc
    required_datasets = tuple(case.get("required_datasets") or ())
    turns: list[dict[str, Any]] = []
    prior_topic_id: str | None = None
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
        if turn_record["scenario"]:
            turn_record["runtime_authority"] = _runtime_audit_package(effective)
            turn_record["obligation_review"] = review_case_obligations(
                turn_record,
                RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH),
                coverage_authority=coverage_authority,
            )
        turn_record["strict_quality_failed"] = bool(
            strict_quality and _strict_quality_failed(turn_record)
        )
        turns.append(turn_record)
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
