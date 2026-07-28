from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.temporal_comparison import (
    EffectiveTemporalComparison,
    TemporalComparisonContractError,
)


CAPABILITY_TASK_DECLARED_BUDGET_UNITS = 1


class PlanAuthorityContractError(ValueError):
    pass


_AUTHORITY_COVERAGE_STATES = frozenset(
    {"claim_ready", "context_only", "unavailable", "missing_contract"}
)
_PROPOSAL_ADMISSION_STATES = frozenset({"admitted", "rejected", "deferred"})
_PROPOSAL_ITEM_KINDS = frozenset(
    {"analysis_axis", "hypothesis", "priority", "assumption"}
)
_OBLIGATION_ROLES = frozenset({"user_required", "analyst_auxiliary"})
_EVIDENCE_REQUIREMENT_OPERATORS = frozenset({"any_of"})
_OBLIGATION_SUBJECT_FIELDS = {
    "user_required": frozenset(
        {"target_metric_ref", "scope", "outcome_refs", "goal_refs"}
    ),
    "analyst_auxiliary": frozenset(
        {
            "planner_proposal_ref",
            "proposal_item_ref",
            "target_metric_refs",
            "scope",
            "goal_refs",
        }
    ),
}
_ANALYSIS_AXIS_ROLES = frozenset({"required", "disclosure", "auxiliary", "conditional"})
_DEGRADATION_POLICY_FIELDS = frozenset(
    {"missing_required_input", "missing_optional_input", "incomplete_input"}
)
_GOVERNOR_INPUT_VALUES = {
    "expected_information_gain": frozenset(
        {"obligation_closing", "hypothesis_testing", "context_enrichment"}
    ),
    "materiality": frozenset({"user_required", "analyst_auxiliary", "contextual"}),
    "actionability": frozenset(
        {"decision_supporting", "explanation_supporting", "diagnostic"}
    ),
    "statistical_risk": frozenset({"contract_bounded", "multiplicity_sensitive"}),
}
_CONTEXT_WINDOW_RELATIONS = frozenset(
    {"trailing_complete_periods", "evaluation_range"}
)
_CONTEXT_WINDOW_UNITS = frozenset({"day", "week", "month", "quarter"})
_CONTEXT_WINDOW_SPEC_REF_PREFIX = "context-window-spec:sha256:"


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _freeze(value: Any) -> Any:
    normalized = canonical_value(value)
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item) for item in normalized)
    return normalized


def _required_string(value: Any, error: str) -> str:
    if not _is_required_string(value):
        raise PlanAuthorityContractError(error)
    return str(value)


def _is_required_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _positive_integer(value: Any, error: str) -> int:
    if type(value) is not int or value < 1:
        raise PlanAuthorityContractError(error)
    return value


def _governor_inputs(value: Any, error: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_GOVERNOR_INPUT_VALUES):
        raise PlanAuthorityContractError(error)
    normalized = {
        key: _required_string(value.get(key), error) for key in _GOVERNOR_INPUT_VALUES
    }
    if any(
        normalized[key] not in allowed
        for key, allowed in _GOVERNOR_INPUT_VALUES.items()
    ):
        raise PlanAuthorityContractError(error)
    return _freeze(normalized)


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PlanAuthorityContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise PlanAuthorityContractError(error)
    if len(normalized) != len(set(normalized)):
        raise PlanAuthorityContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _claim_obligation_subject(
    role: str,
    value: Any,
) -> Mapping[str, Any]:
    expected_fields = _OBLIGATION_SUBJECT_FIELDS[role]
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise PlanAuthorityContractError("claim_obligation_subject_invalid")
    scope = value.get("scope")
    if not isinstance(scope, Mapping) or not scope:
        raise PlanAuthorityContractError("claim_obligation_subject_invalid")
    if role == "user_required":
        normalized = {
            "target_metric_ref": _required_string(
                value.get("target_metric_ref"),
                "claim_obligation_subject_invalid",
            ),
            "scope": _freeze(scope),
            "outcome_refs": _string_tuple(
                value.get("outcome_refs"),
                "claim_obligation_subject_invalid",
                allow_empty=False,
            ),
            "goal_refs": _string_tuple(
                value.get("goal_refs"),
                "claim_obligation_subject_invalid",
                allow_empty=False,
            ),
        }
    else:
        normalized = {
            "planner_proposal_ref": _required_string(
                value.get("planner_proposal_ref"),
                "claim_obligation_subject_invalid",
            ),
            "proposal_item_ref": _required_string(
                value.get("proposal_item_ref"),
                "claim_obligation_subject_invalid",
            ),
            "target_metric_refs": _string_tuple(
                value.get("target_metric_refs"),
                "claim_obligation_subject_invalid",
                allow_empty=False,
            ),
            "scope": _freeze(scope),
            "goal_refs": _string_tuple(
                value.get("goal_refs"),
                "claim_obligation_subject_invalid",
                allow_empty=False,
            ),
        }
    return _freeze(normalized)


def _mapping_tuple(value: Any, error: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PlanAuthorityContractError(error)
    if any(not isinstance(item, Mapping) for item in value):
        raise PlanAuthorityContractError(error)
    return tuple(_freeze(item) for item in value)


def _contract_versions(value: Any, error: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PlanAuthorityContractError(error)
    normalized: dict[str, str] = {}
    for key, item in value.items():
        normalized[_required_string(key, error)] = _required_string(item, error)
    return _freeze(dict(sorted(normalized.items())))


def _execution_policy(value: Any, error: str) -> Mapping[str, Any]:
    expected = {"degradation_policy", "integrity_failure", "input_states"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise PlanAuthorityContractError(error)
    degradation = value.get("degradation_policy")
    if (
        not isinstance(degradation, Mapping)
        or not degradation
        or "missing_required_input" not in degradation
        or set(degradation) - _DEGRADATION_POLICY_FIELDS
    ):
        raise PlanAuthorityContractError(error)
    normalized_degradation = {
        _required_string(key, error): _required_string(item, error)
        for key, item in degradation.items()
    }
    if value.get("integrity_failure") != "fail_closed":
        raise PlanAuthorityContractError(error)
    raw_states = _mapping_tuple(value.get("input_states"), error)
    normalized_states: list[Mapping[str, Any]] = []
    input_refs: set[str] = set()
    for state in raw_states:
        if set(state) != {"input_ref", "availability", "limitation_ref"}:
            raise PlanAuthorityContractError(error)
        input_ref = _required_string(state.get("input_ref"), error)
        if input_ref in input_refs:
            raise PlanAuthorityContractError(error)
        input_refs.add(input_ref)
        availability = state.get("availability")
        if availability not in _AUTHORITY_COVERAGE_STATES:
            raise PlanAuthorityContractError(error)
        normalized_states.append(
            _freeze(
                {
                    "input_ref": input_ref,
                    "availability": availability,
                    "limitation_ref": _optional_string(
                        state.get("limitation_ref"), error
                    ),
                }
            )
        )
    normalized_states.sort(key=lambda item: str(item["input_ref"]))
    return _freeze(
        {
            "degradation_policy": dict(sorted(normalized_degradation.items())),
            "integrity_failure": "fail_closed",
            "input_states": tuple(normalized_states),
        }
    )


def _aware_iso(value: str | datetime, error: str) -> str:
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
    except ValueError as exc:
        raise PlanAuthorityContractError(error) from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise PlanAuthorityContractError(error)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AuthorityContext:
    authority_context_ref: str
    run_attempt_id: str
    actual_as_of: str
    release_refs: tuple[str, ...]
    snapshot_refs: tuple[str, ...]
    dataset_coverage: tuple[Mapping[str, Any], ...]
    contract_versions: Mapping[str, str]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        actual_as_of: str | datetime,
        release_refs: Sequence[str],
        snapshot_refs: Sequence[str],
        dataset_coverage: Sequence[Mapping[str, Any]],
        contract_versions: Mapping[str, str],
    ) -> "AuthorityContext":
        run_attempt_id = _required_string(
            run_attempt_id, "authority_context_run_attempt_id_invalid"
        )
        actual_as_of = _aware_iso(
            actual_as_of, "authority_context_actual_as_of_invalid"
        )
        releases = _string_tuple(
            release_refs, "authority_context_release_refs_invalid", sort=True
        )
        snapshots = _string_tuple(
            snapshot_refs, "authority_context_snapshot_refs_invalid", sort=True
        )
        versions = _contract_versions(
            contract_versions, "authority_context_contract_versions_invalid"
        )
        raw_coverage = _mapping_tuple(
            dataset_coverage, "authority_context_dataset_coverage_invalid"
        )
        normalized_coverage: list[Mapping[str, Any]] = []
        dataset_ids: set[str] = set()
        covered_releases: set[str] = set()
        covered_snapshots: set[str] = set()
        expected_fields = {
            "dataset_id",
            "availability",
            "release_ref",
            "snapshot_refs",
            "limitation_ref",
        }
        for item in raw_coverage:
            if set(item) != expected_fields:
                raise PlanAuthorityContractError(
                    "authority_context_dataset_coverage_invalid"
                )
            dataset_id = _required_string(
                item.get("dataset_id"),
                "authority_context_dataset_coverage_invalid",
            )
            if dataset_id in dataset_ids:
                raise PlanAuthorityContractError(
                    "authority_context_dataset_coverage_duplicated"
                )
            dataset_ids.add(dataset_id)
            availability = item.get("availability")
            if availability not in _AUTHORITY_COVERAGE_STATES:
                raise PlanAuthorityContractError(
                    "authority_context_dataset_coverage_invalid"
                )
            release_ref = _optional_string(
                item.get("release_ref"),
                "authority_context_dataset_coverage_invalid",
            )
            item_snapshots = _string_tuple(
                item.get("snapshot_refs"),
                "authority_context_dataset_coverage_invalid",
                sort=True,
            )
            limitation_ref = _optional_string(
                item.get("limitation_ref"),
                "authority_context_dataset_coverage_invalid",
            )
            if availability in {"claim_ready", "context_only"}:
                if release_ref is None or not item_snapshots:
                    raise PlanAuthorityContractError(
                        "authority_context_available_dataset_unbound"
                    )
                covered_releases.add(release_ref)
                covered_snapshots.update(item_snapshots)
            elif release_ref is not None or item_snapshots:
                raise PlanAuthorityContractError(
                    "authority_context_unavailable_dataset_bound"
                )
            normalized_coverage.append(
                _freeze(
                    {
                        "dataset_id": dataset_id,
                        "availability": availability,
                        "release_ref": release_ref,
                        "snapshot_refs": item_snapshots,
                        "limitation_ref": limitation_ref,
                    }
                )
            )
        if covered_releases != set(releases):
            raise PlanAuthorityContractError(
                "authority_context_release_closure_invalid"
            )
        if covered_snapshots != set(snapshots):
            raise PlanAuthorityContractError(
                "authority_context_snapshot_closure_invalid"
            )
        normalized_coverage.sort(key=lambda item: str(item["dataset_id"]))
        body = {
            "run_attempt_id": run_attempt_id,
            "actual_as_of": actual_as_of,
            "release_refs": releases,
            "snapshot_refs": snapshots,
            "dataset_coverage": tuple(normalized_coverage),
            "contract_versions": versions,
        }
        digest = canonical_digest(body)
        return cls(
            authority_context_ref="authority-context:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthorityContext":
        fields = {
            "authority_context_ref",
            "run_attempt_id",
            "actual_as_of",
            "release_refs",
            "snapshot_refs",
            "dataset_coverage",
            "contract_versions",
            "content_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("authority_context_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in fields
                if key not in {"authority_context_ref", "content_digest"}
            }
        )
        if rebuilt.authority_context_ref != payload.get("authority_context_ref"):
            raise PlanAuthorityContractError("authority_context_ref_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise PlanAuthorityContractError("authority_context_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class PlannerProposal:
    planner_proposal_id: str
    run_attempt_id: str
    intent_revision_id: str
    decision_refs: tuple[str, ...]
    authority_context_ref: str
    issue_tree: tuple[Mapping[str, Any], ...]
    auxiliary_axes: tuple[Mapping[str, Any], ...]
    hypotheses: tuple[Mapping[str, Any], ...]
    priority_proposals: tuple[Mapping[str, Any], ...]
    assumption_proposals: tuple[Mapping[str, Any], ...]
    raw_provider_response_ref: str
    schema_version: str
    prompt_version: str
    model_version: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        intent_revision_id: str,
        decision_refs: Sequence[str],
        authority_context_ref: str,
        issue_tree: Sequence[Mapping[str, Any]],
        auxiliary_axes: Sequence[Mapping[str, Any]],
        hypotheses: Sequence[Mapping[str, Any]],
        priority_proposals: Sequence[Mapping[str, Any]],
        assumption_proposals: Sequence[Mapping[str, Any]] = (),
        raw_provider_response_ref: str,
        schema_version: str,
        prompt_version: str,
        model_version: str,
    ) -> "PlannerProposal":
        run_attempt_id = _required_string(
            run_attempt_id, "planner_proposal_run_attempt_id_invalid"
        )
        intent_revision_id = _required_string(
            intent_revision_id, "planner_proposal_intent_revision_id_invalid"
        )
        decisions = _string_tuple(
            decision_refs, "planner_proposal_decision_refs_invalid"
        )
        authority_context_ref = _required_string(
            authority_context_ref,
            "planner_proposal_authority_context_ref_invalid",
        )
        issues = _mapping_tuple(issue_tree, "planner_proposal_issue_tree_invalid")
        if not issues:
            raise PlanAuthorityContractError("planner_proposal_issue_tree_invalid")
        issue_ids: set[str] = set()
        for index, issue in enumerate(issues):
            if set(issue) != {
                "issue_id",
                "parent_issue_id",
                "question",
                "target_claim_kind",
            }:
                raise PlanAuthorityContractError("planner_proposal_issue_tree_invalid")
            issue_id = _required_string(
                issue.get("issue_id"), "planner_proposal_issue_tree_invalid"
            )
            if issue_id in issue_ids:
                raise PlanAuthorityContractError(
                    "planner_proposal_issue_tree_duplicated"
                )
            parent = issue.get("parent_issue_id")
            if parent is not None and (
                not isinstance(parent, str) or parent not in issue_ids
            ):
                raise PlanAuthorityContractError(
                    "planner_proposal_issue_tree_parent_invalid"
                )
            if index and parent is None:
                raise PlanAuthorityContractError(
                    "planner_proposal_issue_tree_root_invalid"
                )
            _required_string(
                issue.get("question"), "planner_proposal_issue_tree_invalid"
            )
            _required_string(
                issue.get("target_claim_kind"),
                "planner_proposal_issue_tree_invalid",
            )
            issue_ids.add(issue_id)

        axes = _mapping_tuple(auxiliary_axes, "planner_proposal_auxiliary_axes_invalid")
        hypotheses_value = _mapping_tuple(
            hypotheses, "planner_proposal_hypotheses_invalid"
        )
        assumptions = _mapping_tuple(
            assumption_proposals, "planner_proposal_assumptions_invalid"
        )
        proposal_item_ids: set[str] = set()
        normalized_axes: list[Mapping[str, Any]] = []
        for item in axes:
            if set(item) != {
                "proposal_item_id",
                "axis_id",
                "rationale",
                "supports_claim_kinds",
            }:
                raise PlanAuthorityContractError(
                    "planner_proposal_auxiliary_axes_invalid"
                )
            item_id = _proposal_item_id(
                item, proposal_item_ids, "planner_proposal_auxiliary_axes_invalid"
            )
            normalized_axes.append(
                _freeze(
                    {
                        "proposal_item_id": item_id,
                        "axis_id": _required_string(
                            item.get("axis_id"),
                            "planner_proposal_auxiliary_axes_invalid",
                        ),
                        "rationale": _required_string(
                            item.get("rationale"),
                            "planner_proposal_auxiliary_axes_invalid",
                        ),
                        "supports_claim_kinds": _string_tuple(
                            item.get("supports_claim_kinds"),
                            "planner_proposal_auxiliary_axes_invalid",
                        ),
                    }
                )
            )
        normalized_hypotheses: list[Mapping[str, Any]] = []
        for item in hypotheses_value:
            if set(item) != {
                "proposal_item_id",
                "issue_ref",
                "statement",
                "target_claim_kind",
                "requested_axis_ids",
                "assumption_refs",
            }:
                raise PlanAuthorityContractError("planner_proposal_hypotheses_invalid")
            item_id = _proposal_item_id(
                item, proposal_item_ids, "planner_proposal_hypotheses_invalid"
            )
            normalized_hypotheses.append(
                _freeze(
                    {
                        "proposal_item_id": item_id,
                        "issue_ref": _required_string(
                            item.get("issue_ref"),
                            "planner_proposal_hypotheses_invalid",
                        ),
                        "statement": _required_string(
                            item.get("statement"),
                            "planner_proposal_hypotheses_invalid",
                        ),
                        "target_claim_kind": _required_string(
                            item.get("target_claim_kind"),
                            "planner_proposal_hypotheses_invalid",
                        ),
                        "requested_axis_ids": _string_tuple(
                            item.get("requested_axis_ids"),
                            "planner_proposal_hypotheses_invalid",
                        ),
                        "assumption_refs": _string_tuple(
                            item.get("assumption_refs"),
                            "planner_proposal_hypotheses_invalid",
                        ),
                    }
                )
            )
            if normalized_hypotheses[-1]["issue_ref"] not in issue_ids:
                raise PlanAuthorityContractError(
                    "planner_proposal_hypothesis_issue_ref_invalid"
                )
        normalized_assumptions: list[Mapping[str, Any]] = []
        for item in assumptions:
            if set(item) != {"proposal_item_id", "statement", "affected_refs"}:
                raise PlanAuthorityContractError("planner_proposal_assumptions_invalid")
            item_id = _proposal_item_id(
                item, proposal_item_ids, "planner_proposal_assumptions_invalid"
            )
            normalized_assumptions.append(
                _freeze(
                    {
                        "proposal_item_id": item_id,
                        "statement": _required_string(
                            item.get("statement"),
                            "planner_proposal_assumptions_invalid",
                        ),
                        "affected_refs": _string_tuple(
                            item.get("affected_refs"),
                            "planner_proposal_assumptions_invalid",
                            allow_empty=False,
                        ),
                    }
                )
            )
        priority_values = _mapping_tuple(
            priority_proposals, "planner_proposal_priorities_invalid"
        )
        normalized_priorities: list[Mapping[str, Any]] = []
        for item in priority_values:
            if set(item) != {"proposal_item_id", "target_ref", "rationale"}:
                raise PlanAuthorityContractError("planner_proposal_priorities_invalid")
            item_id = _proposal_item_id(
                item, proposal_item_ids, "planner_proposal_priorities_invalid"
            )
            normalized_priorities.append(
                _freeze(
                    {
                        "proposal_item_id": item_id,
                        "target_ref": _required_string(
                            item.get("target_ref"),
                            "planner_proposal_priorities_invalid",
                        ),
                        "rationale": _required_string(
                            item.get("rationale"),
                            "planner_proposal_priorities_invalid",
                        ),
                    }
                )
            )
        raw_provider_response_ref = _required_string(
            raw_provider_response_ref,
            "planner_proposal_raw_response_ref_invalid",
        )
        schema_version = _required_string(
            schema_version, "planner_proposal_schema_version_invalid"
        )
        prompt_version = _required_string(
            prompt_version, "planner_proposal_prompt_version_invalid"
        )
        model_version = _required_string(
            model_version, "planner_proposal_model_version_invalid"
        )
        body = {
            "run_attempt_id": run_attempt_id,
            "intent_revision_id": intent_revision_id,
            "decision_refs": decisions,
            "authority_context_ref": authority_context_ref,
            "issue_tree": issues,
            "auxiliary_axes": tuple(normalized_axes),
            "hypotheses": tuple(normalized_hypotheses),
            "priority_proposals": tuple(normalized_priorities),
            "assumption_proposals": tuple(normalized_assumptions),
            "raw_provider_response_ref": raw_provider_response_ref,
            "schema_version": schema_version,
            "prompt_version": prompt_version,
            "model_version": model_version,
        }
        digest = canonical_digest(body)
        return cls(
            planner_proposal_id="planner-proposal-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlannerProposal":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("planner_proposal_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in fields
                if key not in {"planner_proposal_id", "content_digest"}
            }
        )
        if rebuilt.planner_proposal_id != payload.get("planner_proposal_id"):
            raise PlanAuthorityContractError("planner_proposal_id_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise PlanAuthorityContractError("planner_proposal_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _proposal_item_id(item: Mapping[str, Any], known: set[str], error: str) -> str:
    item_id = _required_string(item.get("proposal_item_id"), error)
    if item_id in known:
        raise PlanAuthorityContractError("planner_proposal_item_id_duplicated")
    known.add(item_id)
    return item_id


@dataclass(frozen=True)
class ProposalAdmissionRecord:
    proposal_admission_id: str
    planner_proposal_ref: str
    intent_revision_id: str
    decision_refs: tuple[str, ...]
    authority_context_ref: str
    admission_entries: tuple[Mapping[str, Any], ...]
    compiler_version: str
    contract_versions: Mapping[str, str]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        planner_proposal_ref: str,
        intent_revision_id: str,
        decision_refs: Sequence[str],
        authority_context_ref: str,
        admission_entries: Sequence[Mapping[str, Any]],
        compiler_version: str,
        contract_versions: Mapping[str, str],
    ) -> "ProposalAdmissionRecord":
        planner_proposal_ref = _required_string(
            planner_proposal_ref, "proposal_admission_proposal_ref_invalid"
        )
        intent_revision_id = _required_string(
            intent_revision_id, "proposal_admission_intent_revision_id_invalid"
        )
        decisions = _string_tuple(
            decision_refs, "proposal_admission_decision_refs_invalid"
        )
        authority_context_ref = _required_string(
            authority_context_ref,
            "proposal_admission_authority_context_ref_invalid",
        )
        raw_entries = _mapping_tuple(
            admission_entries, "proposal_admission_entries_invalid"
        )
        normalized_entries: list[Mapping[str, Any]] = []
        item_refs: set[str] = set()
        expected = {
            "proposal_item_ref",
            "item_kind",
            "status",
            "reason_code",
            "contract_refs",
            "normalized_execution_ref",
        }
        for item in raw_entries:
            if set(item) != expected:
                raise PlanAuthorityContractError("proposal_admission_entries_invalid")
            item_ref = _required_string(
                item.get("proposal_item_ref"),
                "proposal_admission_entries_invalid",
            )
            if item_ref in item_refs:
                raise PlanAuthorityContractError("proposal_admission_item_duplicated")
            item_refs.add(item_ref)
            item_kind = item.get("item_kind")
            status = item.get("status")
            if item_kind not in _PROPOSAL_ITEM_KINDS:
                raise PlanAuthorityContractError("proposal_admission_entries_invalid")
            if status not in _PROPOSAL_ADMISSION_STATES:
                raise PlanAuthorityContractError("proposal_admission_entries_invalid")
            reason_code = _required_string(
                item.get("reason_code"),
                "proposal_admission_entries_invalid",
            )
            contract_refs = _string_tuple(
                item.get("contract_refs"),
                "proposal_admission_entries_invalid",
            )
            execution_ref = _optional_string(
                item.get("normalized_execution_ref"),
                "proposal_admission_entries_invalid",
            )
            if status == "admitted" and execution_ref is None:
                raise PlanAuthorityContractError(
                    "proposal_admission_admitted_ref_missing"
                )
            if status != "admitted" and execution_ref is not None:
                raise PlanAuthorityContractError(
                    "proposal_admission_rejected_ref_present"
                )
            normalized_entries.append(
                _freeze(
                    {
                        "proposal_item_ref": item_ref,
                        "item_kind": item_kind,
                        "status": status,
                        "reason_code": reason_code,
                        "contract_refs": contract_refs,
                        "normalized_execution_ref": execution_ref,
                    }
                )
            )
        compiler_version = _required_string(
            compiler_version, "proposal_admission_compiler_version_invalid"
        )
        versions = _contract_versions(
            contract_versions, "proposal_admission_contract_versions_invalid"
        )
        body = {
            "planner_proposal_ref": planner_proposal_ref,
            "intent_revision_id": intent_revision_id,
            "decision_refs": decisions,
            "authority_context_ref": authority_context_ref,
            "admission_entries": tuple(normalized_entries),
            "compiler_version": compiler_version,
            "contract_versions": versions,
        }
        digest = canonical_digest(body)
        return cls(
            proposal_admission_id="proposal-admission-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProposalAdmissionRecord":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("proposal_admission_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in fields
                if key not in {"proposal_admission_id", "content_digest"}
            }
        )
        if rebuilt.proposal_admission_id != payload.get("proposal_admission_id"):
            raise PlanAuthorityContractError("proposal_admission_id_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise PlanAuthorityContractError("proposal_admission_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceRequirement:
    operator: str
    evidence_kinds: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        operator: str,
        evidence_kinds: Sequence[str],
    ) -> "EvidenceRequirement":
        if operator not in _EVIDENCE_REQUIREMENT_OPERATORS:
            raise PlanAuthorityContractError("evidence_requirement_operator_invalid")
        return cls(
            operator=operator,
            evidence_kinds=_string_tuple(
                evidence_kinds,
                "evidence_requirement_evidence_kinds_invalid",
                allow_empty=False,
            ),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceRequirement":
        if not isinstance(payload, Mapping) or set(payload) != {
            "operator",
            "evidence_kinds",
        }:
            raise PlanAuthorityContractError("evidence_requirement_shape_invalid")
        return cls.create(
            operator=payload["operator"],
            evidence_kinds=payload["evidence_kinds"],
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimObligation:
    obligation_id: str
    claim_kind: str
    role: str
    subject: Mapping[str, Any]
    evidence_requirement: EvidenceRequirement
    success_policy: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        claim_kind: str,
        role: str,
        subject: Mapping[str, Any],
        evidence_requirement: EvidenceRequirement,
        success_policy: Mapping[str, Any],
    ) -> "ClaimObligation":
        claim_kind = _required_string(claim_kind, "claim_obligation_claim_kind_invalid")
        if role not in _OBLIGATION_ROLES:
            raise PlanAuthorityContractError("claim_obligation_role_invalid")
        subject = _claim_obligation_subject(role, subject)
        if type(evidence_requirement) is not EvidenceRequirement:
            raise PlanAuthorityContractError(
                "claim_obligation_evidence_requirement_invalid"
            )
        requirement = EvidenceRequirement.from_dict(evidence_requirement.to_dict())
        if requirement != evidence_requirement:
            raise PlanAuthorityContractError(
                "claim_obligation_evidence_requirement_invalid"
            )
        if not isinstance(success_policy, Mapping) or not success_policy:
            raise PlanAuthorityContractError("claim_obligation_success_policy_invalid")
        if not _is_required_string(
            success_policy.get("policy")
        ) or not _is_required_string(success_policy.get("minimum_claim_strength")):
            raise PlanAuthorityContractError("claim_obligation_success_policy_invalid")
        policy = _freeze(success_policy)
        body = {
            "claim_kind": claim_kind,
            "role": role,
            "subject": subject,
            "evidence_requirement": requirement,
            "success_policy": policy,
        }
        digest = canonical_digest(body)
        return cls(
            obligation_id="claim-obligation-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimObligation":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("claim_obligation_shape_invalid")
        rebuilt = cls.create(
            claim_kind=payload["claim_kind"],
            role=payload["role"],
            subject=payload["subject"],
            evidence_requirement=EvidenceRequirement.from_dict(
                payload["evidence_requirement"]
            ),
            success_policy=payload["success_policy"],
        )
        if rebuilt.obligation_id != payload.get("obligation_id"):
            raise PlanAuthorityContractError("claim_obligation_id_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise PlanAuthorityContractError("claim_obligation_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class AcceptedQuestionIssue:
    """One accepted Planner question and its non-blocking answer contract."""

    issue_ref: str
    parent_issue_ref: str | None
    business_question: str
    role: str
    target_claim_kind: str
    answer_contract: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        issue_ref: str,
        parent_issue_ref: str | None,
        business_question: str,
        role: str,
        target_claim_kind: str,
        answer_contract: Mapping[str, Any],
    ) -> "AcceptedQuestionIssue":
        issue_ref = _required_string(
            issue_ref, "accepted_question_issue_ref_invalid"
        )
        parent_issue_ref = _optional_string(
            parent_issue_ref, "accepted_question_parent_ref_invalid"
        )
        if role not in {"primary", "supporting"}:
            raise PlanAuthorityContractError("accepted_question_role_invalid")
        if not isinstance(answer_contract, Mapping):
            raise PlanAuthorityContractError(
                "accepted_question_answer_contract_invalid"
            )
        contract = _freeze(answer_contract)
        required_elements = _string_tuple(
            contract.get("required_elements"),
            "accepted_question_answer_contract_invalid",
            allow_empty=False,
        )
        if (
            contract.get("contract_version") != "question-answer-contract.v1"
            or contract.get("completion_policy")
            != "direct_answer_or_explicitly_unresolved"
            or contract.get("blocking") is not False
            or required_elements
            != (
                "direct_answer",
                "evidence_basis",
                "local_boundary_when_limited",
            )
        ):
            raise PlanAuthorityContractError(
                "accepted_question_answer_contract_invalid"
            )
        body = {
            "issue_ref": issue_ref,
            "parent_issue_ref": parent_issue_ref,
            "business_question": _required_string(
                business_question, "accepted_question_business_question_invalid"
            ),
            "role": role,
            "target_claim_kind": _required_string(
                target_claim_kind, "accepted_question_claim_kind_invalid"
            ),
            "answer_contract": contract,
        }
        return cls(content_digest=canonical_digest(body), **body)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AcceptedQuestionIssue":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("accepted_question_shape_invalid")
        rebuilt = cls.create(
            issue_ref=payload["issue_ref"],
            parent_issue_ref=payload["parent_issue_ref"],
            business_question=payload["business_question"],
            role=payload["role"],
            target_claim_kind=payload["target_claim_kind"],
            answer_contract=payload["answer_contract"],
        )
        if rebuilt.to_dict() != _plain(payload):
            raise PlanAuthorityContractError("accepted_question_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class AnalysisAxis:
    analysis_axis_ref: str
    axis_id: str
    role: str
    axis_kind: str
    target_metric_refs: tuple[str, ...]
    metric_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    context_source_refs: tuple[str, ...]
    capability_refs: tuple[str, ...]
    reconciliation_group: str
    selection_policy: str
    source_refs: tuple[str, ...]
    goal_refs: tuple[str, ...]
    supports_obligation_ids: tuple[str, ...]
    proposal_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        axis_id: str,
        role: str,
        axis_kind: str,
        target_metric_refs: Sequence[str],
        metric_refs: Sequence[str],
        dimension_refs: Sequence[str],
        context_source_refs: Sequence[str],
        capability_refs: Sequence[str],
        reconciliation_group: str,
        selection_policy: str,
        source_refs: Sequence[str],
        goal_refs: Sequence[str],
        supports_obligation_ids: Sequence[str] = (),
        proposal_refs: Sequence[str] = (),
    ) -> "AnalysisAxis":
        axis_id = _required_string(axis_id, "analysis_axis_id_invalid")
        if role not in _ANALYSIS_AXIS_ROLES:
            raise PlanAuthorityContractError("analysis_axis_role_invalid")
        body = {
            "axis_id": axis_id,
            "role": role,
            "axis_kind": _required_string(axis_kind, "analysis_axis_kind_invalid"),
            "target_metric_refs": _string_tuple(
                target_metric_refs,
                "analysis_axis_target_metric_refs_invalid",
                allow_empty=False,
            ),
            "metric_refs": _string_tuple(
                metric_refs, "analysis_axis_metric_refs_invalid"
            ),
            "dimension_refs": _string_tuple(
                dimension_refs, "analysis_axis_dimension_refs_invalid"
            ),
            "context_source_refs": _string_tuple(
                context_source_refs,
                "analysis_axis_context_source_refs_invalid",
            ),
            "capability_refs": _string_tuple(
                capability_refs,
                "analysis_axis_capability_refs_invalid",
                allow_empty=False,
            ),
            "reconciliation_group": _required_string(
                reconciliation_group,
                "analysis_axis_reconciliation_group_invalid",
            ),
            "selection_policy": _required_string(
                selection_policy, "analysis_axis_selection_policy_invalid"
            ),
            "source_refs": _string_tuple(
                source_refs, "analysis_axis_source_refs_invalid", allow_empty=False
            ),
            "goal_refs": _string_tuple(
                goal_refs, "analysis_axis_goal_refs_invalid", allow_empty=False
            ),
            "supports_obligation_ids": _string_tuple(
                supports_obligation_ids,
                "analysis_axis_obligation_refs_invalid",
            ),
            "proposal_refs": _string_tuple(
                proposal_refs, "analysis_axis_proposal_refs_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            analysis_axis_ref="analysis-axis-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AnalysisAxis":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("analysis_axis_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in fields
                if key not in {"analysis_axis_ref", "content_digest"}
            }
        )
        if rebuilt.analysis_axis_ref != payload.get("analysis_axis_ref"):
            raise PlanAuthorityContractError("analysis_axis_ref_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise PlanAuthorityContractError("analysis_axis_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class CapabilityTask:
    task_id: str
    task_key: str
    plan_revision_id: str
    authority_context_ref: str
    capability_id: str
    normalized_input_refs: tuple[str, ...]
    dependency_task_ids: tuple[str, ...]
    obligation_edges: tuple[Mapping[str, Any], ...]
    supports_obligation_ids: tuple[str, ...]
    execution_rank: int
    declared_budget_units: int
    governor_inputs: Mapping[str, str]
    execution_policy: Mapping[str, Any]
    idempotency_key: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        task_key: str,
        plan_revision_id: str,
        authority_context_ref: str,
        capability_id: str,
        normalized_input_refs: Sequence[str],
        dependency_task_ids: Sequence[str],
        obligation_edges: Sequence[Mapping[str, Any]],
        execution_rank: int,
        declared_budget_units: int,
        governor_inputs: Mapping[str, Any],
        execution_policy: Mapping[str, Any],
        contract_versions: Mapping[str, str],
    ) -> "CapabilityTask":
        task_key = _required_string(task_key, "capability_task_key_invalid")
        plan_revision_id = _required_string(
            plan_revision_id, "capability_task_plan_revision_id_invalid"
        )
        authority_context_ref = _required_string(
            authority_context_ref,
            "capability_task_authority_context_ref_invalid",
        )
        capability_id = _required_string(
            capability_id, "capability_task_capability_id_invalid"
        )
        inputs = _string_tuple(
            normalized_input_refs,
            "capability_task_input_refs_invalid",
            allow_empty=False,
        )
        dependencies = _string_tuple(
            dependency_task_ids, "capability_task_dependency_refs_invalid"
        )
        raw_edges = _mapping_tuple(
            obligation_edges, "capability_task_obligation_edges_invalid"
        )
        normalized_edges: list[Mapping[str, Any]] = []
        edge_refs: list[str] = []
        for edge in raw_edges:
            if set(edge) != {"obligation_id", "required"}:
                raise PlanAuthorityContractError(
                    "capability_task_obligation_edges_invalid"
                )
            obligation_id = _required_string(
                edge.get("obligation_id"),
                "capability_task_obligation_edges_invalid",
            )
            required = edge.get("required")
            if type(required) is not bool or obligation_id in edge_refs:
                raise PlanAuthorityContractError(
                    "capability_task_obligation_edges_invalid"
                )
            edge_refs.append(obligation_id)
            normalized_edges.append(
                _freeze({"obligation_id": obligation_id, "required": required})
            )
        policy = _execution_policy(
            execution_policy, "capability_task_execution_policy_invalid"
        )
        rank = _positive_integer(
            execution_rank, "capability_task_execution_rank_invalid"
        )
        budget_units = _positive_integer(
            declared_budget_units,
            "capability_task_declared_budget_units_invalid",
        )
        normalized_governor_inputs = _governor_inputs(
            governor_inputs,
            "capability_task_governor_inputs_invalid",
        )
        versions = _contract_versions(
            contract_versions, "capability_task_contract_versions_invalid"
        )
        identity = {
            "task_key": task_key,
            "plan_revision_id": plan_revision_id,
            "authority_context_ref": authority_context_ref,
            "capability_id": capability_id,
            "normalized_input_refs": inputs,
        }
        task_id = "capability-task-" + canonical_digest(identity)[:24]
        idempotency_key = canonical_digest(
            {
                "plan_revision_id": plan_revision_id,
                "task_id": task_id,
                "normalized_input_refs": inputs,
                "authority_context_ref": authority_context_ref,
                "contract_versions": versions,
            }
        )
        body = {
            **identity,
            "dependency_task_ids": dependencies,
            "obligation_edges": tuple(normalized_edges),
            "supports_obligation_ids": tuple(edge_refs),
            "execution_rank": rank,
            "declared_budget_units": budget_units,
            "governor_inputs": normalized_governor_inputs,
            "execution_policy": policy,
            "idempotency_key": idempotency_key,
        }
        return cls(
            task_id=task_id,
            content_digest=canonical_digest(body),
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        contract_versions: Mapping[str, str] | None = None,
    ) -> "CapabilityTask":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("capability_task_shape_invalid")
        if contract_versions is None:
            raise PlanAuthorityContractError(
                "capability_task_contract_versions_required"
            )
        rebuilt = cls.create(
            task_key=payload["task_key"],
            plan_revision_id=payload["plan_revision_id"],
            authority_context_ref=payload["authority_context_ref"],
            capability_id=payload["capability_id"],
            normalized_input_refs=payload["normalized_input_refs"],
            dependency_task_ids=payload["dependency_task_ids"],
            obligation_edges=payload["obligation_edges"],
            execution_rank=payload["execution_rank"],
            declared_budget_units=payload["declared_budget_units"],
            governor_inputs=payload["governor_inputs"],
            execution_policy=payload["execution_policy"],
            contract_versions=contract_versions,
        )
        for field in ("task_id", "idempotency_key", "content_digest"):
            if getattr(rebuilt, field) != payload.get(field):
                raise PlanAuthorityContractError(f"capability_task_{field}_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class PlanContextWindowSpec:
    capability_id: str
    relation: str
    unit: str
    count: int

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        relation: str,
        unit: str,
        count: int,
    ) -> "PlanContextWindowSpec":
        capability_id = _required_string(
            capability_id,
            "plan_context_window_capability_id_invalid",
        )
        relation = _required_string(
            relation,
            "plan_context_window_relation_invalid",
        )
        unit = _required_string(unit, "plan_context_window_unit_invalid")
        if relation not in _CONTEXT_WINDOW_RELATIONS:
            raise PlanAuthorityContractError("plan_context_window_relation_invalid")
        if unit not in _CONTEXT_WINDOW_UNITS:
            raise PlanAuthorityContractError("plan_context_window_unit_invalid")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise PlanAuthorityContractError("plan_context_window_count_invalid")
        return cls(
            capability_id=capability_id,
            relation=relation,
            unit=unit,
            count=count,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlanContextWindowSpec":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("plan_context_window_shape_invalid")
        return cls.create(**{key: payload[key] for key in fields})

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @property
    def normalized_input_ref(self) -> str:
        return _CONTEXT_WINDOW_SPEC_REF_PREFIX + canonical_digest(self.to_dict())


@dataclass(frozen=True)
class PlanRevision:
    plan_revision_id: str
    run_attempt_id: str
    supersedes_plan_revision_id: str | None
    intent_revision_id: str
    decision_refs: tuple[str, ...]
    authority_context_ref: str
    planner_proposal_ref: str
    proposal_admission_ref: str
    temporal_authority: EffectiveTemporalComparison
    resolved_window_refs: tuple[str, ...]
    context_window_specs: tuple[PlanContextWindowSpec, ...]
    claim_obligations: tuple[ClaimObligation, ...]
    accepted_question_graph: tuple[AcceptedQuestionIssue, ...]
    analysis_axes: tuple[AnalysisAxis, ...]
    capability_tasks: tuple[CapabilityTask, ...]
    assumption_refs: tuple[str, ...]
    budget_policy_ref: str
    contract_versions: Mapping[str, str]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        supersedes_plan_revision_id: str | None,
        intent_revision_id: str,
        decision_refs: Sequence[str],
        authority_context_ref: str,
        planner_proposal_ref: str,
        proposal_admission_ref: str,
        temporal_authority: EffectiveTemporalComparison | Mapping[str, Any],
        resolved_window_refs: Sequence[str],
        context_window_specs: Sequence[PlanContextWindowSpec | Mapping[str, Any]],
        claim_obligations: Sequence[ClaimObligation | Mapping[str, Any]],
        analysis_axes: Sequence[AnalysisAxis | Mapping[str, Any]],
        capability_task_specs: Sequence[Mapping[str, Any]],
        assumption_refs: Sequence[str],
        budget_policy_ref: str,
        contract_versions: Mapping[str, str],
        accepted_question_graph: Sequence[
            AcceptedQuestionIssue | Mapping[str, Any]
        ] = (),
    ) -> "PlanRevision":
        run_attempt_id = _required_string(
            run_attempt_id, "plan_revision_run_attempt_id_invalid"
        )
        supersedes_plan_revision_id = _optional_string(
            supersedes_plan_revision_id,
            "plan_revision_supersedes_invalid",
        )
        intent_revision_id = _required_string(
            intent_revision_id, "plan_revision_intent_revision_id_invalid"
        )
        decisions = _string_tuple(decision_refs, "plan_revision_decision_refs_invalid")
        authority_context_ref = _required_string(
            authority_context_ref, "plan_revision_authority_context_ref_invalid"
        )
        planner_proposal_ref = _required_string(
            planner_proposal_ref, "plan_revision_planner_proposal_ref_invalid"
        )
        proposal_admission_ref = _required_string(
            proposal_admission_ref,
            "plan_revision_proposal_admission_ref_invalid",
        )
        try:
            temporal = (
                temporal_authority
                if isinstance(temporal_authority, EffectiveTemporalComparison)
                else EffectiveTemporalComparison.from_dict(temporal_authority)
            )
        except (TypeError, TemporalComparisonContractError) as exc:
            raise PlanAuthorityContractError(
                "plan_revision_temporal_authority_invalid"
            ) from exc
        if temporal.mode == "unresolved" or not temporal.has_physical_target:
            raise PlanAuthorityContractError(
                "plan_revision_temporal_authority_unresolved"
            )
        windows = _string_tuple(
            resolved_window_refs,
            "plan_revision_window_refs_invalid",
            allow_empty=False,
        )
        if windows != temporal.resolved_window_refs:
            raise PlanAuthorityContractError(
                "plan_revision_temporal_window_refs_mismatch"
            )
        if temporal.source == "decision" and not set(
            temporal.decision_refs
        ).issubset(decisions):
            raise PlanAuthorityContractError(
                "plan_revision_temporal_decision_ref_missing"
            )
        context_specs = tuple(
            item
            if isinstance(item, PlanContextWindowSpec)
            else PlanContextWindowSpec.from_dict(item)
            for item in context_window_specs
        )
        if len({item.capability_id for item in context_specs}) != len(context_specs):
            raise PlanAuthorityContractError(
                "plan_revision_context_window_capability_duplicated"
            )
        obligations = tuple(
            item
            if isinstance(item, ClaimObligation)
            else ClaimObligation.from_dict(item)
            for item in claim_obligations
        )
        if not obligations or len({item.obligation_id for item in obligations}) != len(
            obligations
        ):
            raise PlanAuthorityContractError("plan_revision_obligations_invalid")
        question_graph = tuple(
            item
            if isinstance(item, AcceptedQuestionIssue)
            else AcceptedQuestionIssue.from_dict(item)
            for item in accepted_question_graph
        )
        if len({item.issue_ref for item in question_graph}) != len(question_graph):
            raise PlanAuthorityContractError(
                "plan_revision_question_graph_duplicated"
            )
        accepted_issue_refs: set[str] = set()
        for index, issue in enumerate(question_graph):
            if index == 0:
                if issue.role != "primary" or issue.parent_issue_ref is not None:
                    raise PlanAuthorityContractError(
                        "plan_revision_question_graph_root_invalid"
                    )
            elif (
                issue.role != "supporting"
                or issue.parent_issue_ref not in accepted_issue_refs
            ):
                raise PlanAuthorityContractError(
                    "plan_revision_question_graph_parent_invalid"
                )
            accepted_issue_refs.add(issue.issue_ref)
        obligation_issue_refs = {
            str(item.success_policy["issue_ref"])
            for item in obligations
            if item.success_policy.get("issue_ref") is not None
        }
        if question_graph and obligation_issue_refs - accepted_issue_refs:
            raise PlanAuthorityContractError(
                "plan_revision_obligation_issue_ref_invalid"
            )
        axes = tuple(
            item if isinstance(item, AnalysisAxis) else AnalysisAxis.from_dict(item)
            for item in analysis_axes
        )
        if not axes or len({item.axis_id for item in axes}) != len(axes):
            raise PlanAuthorityContractError("plan_revision_axes_invalid")
        obligation_ids = {item.obligation_id for item in obligations}
        if any(set(axis.supports_obligation_ids) - obligation_ids for axis in axes):
            raise PlanAuthorityContractError(
                "plan_revision_axis_obligation_ref_invalid"
            )
        assumptions = _string_tuple(
            assumption_refs, "plan_revision_assumption_refs_invalid"
        )
        budget_policy_ref = _required_string(
            budget_policy_ref, "plan_revision_budget_policy_ref_invalid"
        )
        versions = _contract_versions(
            contract_versions, "plan_revision_contract_versions_invalid"
        )
        normalized_specs = _normalize_task_specs(
            capability_task_specs, obligation_ids=obligation_ids
        )
        _validate_context_window_task_refs(context_specs, normalized_specs)
        seed_body = {
            "run_attempt_id": run_attempt_id,
            "supersedes_plan_revision_id": supersedes_plan_revision_id,
            "intent_revision_id": intent_revision_id,
            "decision_refs": decisions,
            "authority_context_ref": authority_context_ref,
            "planner_proposal_ref": planner_proposal_ref,
            "proposal_admission_ref": proposal_admission_ref,
            "temporal_authority": temporal.to_dict(),
            "resolved_window_refs": windows,
            "context_window_specs": tuple(item.to_dict() for item in context_specs),
            "claim_obligations": tuple(item.to_dict() for item in obligations),
            "accepted_question_graph": tuple(
                item.to_dict() for item in question_graph
            ),
            "analysis_axes": tuple(item.to_dict() for item in axes),
            "capability_task_specs": normalized_specs,
            "assumption_refs": assumptions,
            "budget_policy_ref": budget_policy_ref,
            "contract_versions": versions,
        }
        plan_revision_id = "plan-revision-" + canonical_digest(seed_body)[:24]
        task_id_by_key = {
            str(spec["task_key"]): "capability-task-"
            + canonical_digest(
                {
                    "task_key": spec["task_key"],
                    "plan_revision_id": plan_revision_id,
                    "authority_context_ref": authority_context_ref,
                    "capability_id": spec["capability_id"],
                    "normalized_input_refs": spec["normalized_input_refs"],
                }
            )[:24]
            for spec in normalized_specs
        }
        tasks = tuple(
            CapabilityTask.create(
                task_key=str(spec["task_key"]),
                plan_revision_id=plan_revision_id,
                authority_context_ref=authority_context_ref,
                capability_id=str(spec["capability_id"]),
                normalized_input_refs=spec["normalized_input_refs"],
                dependency_task_ids=tuple(
                    task_id_by_key[str(key)] for key in spec["dependency_task_keys"]
                ),
                obligation_edges=spec["obligation_edges"],
                execution_rank=spec["execution_rank"],
                declared_budget_units=spec["declared_budget_units"],
                governor_inputs=spec["governor_inputs"],
                execution_policy=spec["execution_policy"],
                contract_versions=versions,
            )
            for spec in normalized_specs
        )
        _validate_task_dag(tasks)
        required_obligations = {
            item.obligation_id
            for item in obligations
            if item.role == "user_required"
            and item.success_policy.get("execution_route_available", True) is not False
        }
        required_edges = {
            str(edge["obligation_id"])
            for task in tasks
            for edge in task.obligation_edges
            if bool(edge["required"])
        }
        if not required_obligations.issubset(required_edges):
            raise PlanAuthorityContractError(
                "plan_revision_required_obligation_uncovered"
            )
        body = {
            "run_attempt_id": run_attempt_id,
            "supersedes_plan_revision_id": supersedes_plan_revision_id,
            "intent_revision_id": intent_revision_id,
            "decision_refs": decisions,
            "authority_context_ref": authority_context_ref,
            "planner_proposal_ref": planner_proposal_ref,
            "proposal_admission_ref": proposal_admission_ref,
            "temporal_authority": temporal,
            "resolved_window_refs": windows,
            "context_window_specs": context_specs,
            "claim_obligations": obligations,
            "accepted_question_graph": question_graph,
            "analysis_axes": axes,
            "capability_tasks": tasks,
            "assumption_refs": assumptions,
            "budget_policy_ref": budget_policy_ref,
            "contract_versions": versions,
        }
        digest_body = {
            **body,
            "temporal_authority": temporal.to_dict(),
        }
        return cls(
            plan_revision_id=plan_revision_id,
            content_digest=canonical_digest(digest_body),
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlanRevision":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise PlanAuthorityContractError("plan_revision_shape_invalid")
        versions = payload["contract_versions"]
        obligations = tuple(
            ClaimObligation.from_dict(item) for item in payload["claim_obligations"]
        )
        axes = tuple(AnalysisAxis.from_dict(item) for item in payload["analysis_axes"])
        tasks = tuple(
            CapabilityTask.from_dict(item, contract_versions=versions)
            for item in payload["capability_tasks"]
        )
        key_by_id = {task.task_id: task.task_key for task in tasks}
        if any(
            dependency_id not in key_by_id
            for task in tasks
            for dependency_id in task.dependency_task_ids
        ):
            raise PlanAuthorityContractError("plan_revision_task_dependency_unknown")
        specs = tuple(
            {
                "task_key": task.task_key,
                "capability_id": task.capability_id,
                "normalized_input_refs": task.normalized_input_refs,
                "dependency_task_keys": tuple(
                    key_by_id[item] for item in task.dependency_task_ids
                ),
                "obligation_edges": task.obligation_edges,
                "execution_rank": task.execution_rank,
                "declared_budget_units": task.declared_budget_units,
                "governor_inputs": task.governor_inputs,
                "execution_policy": task.execution_policy,
            }
            for task in tasks
        )
        rebuilt = cls.create(
            run_attempt_id=payload["run_attempt_id"],
            supersedes_plan_revision_id=payload["supersedes_plan_revision_id"],
            intent_revision_id=payload["intent_revision_id"],
            decision_refs=payload["decision_refs"],
            authority_context_ref=payload["authority_context_ref"],
            planner_proposal_ref=payload["planner_proposal_ref"],
            proposal_admission_ref=payload["proposal_admission_ref"],
            temporal_authority=payload["temporal_authority"],
            resolved_window_refs=payload["resolved_window_refs"],
            context_window_specs=payload["context_window_specs"],
            claim_obligations=obligations,
            analysis_axes=axes,
            capability_task_specs=specs,
            assumption_refs=payload["assumption_refs"],
            budget_policy_ref=payload["budget_policy_ref"],
            contract_versions=versions,
            accepted_question_graph=payload["accepted_question_graph"],
        )
        if rebuilt.plan_revision_id != payload.get("plan_revision_id"):
            raise PlanAuthorityContractError("plan_revision_id_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise PlanAuthorityContractError("plan_revision_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        payload = _plain(self)
        payload["temporal_authority"] = self.temporal_authority.to_dict()
        return payload

    @property
    def executable(self) -> bool:
        return True


def _normalize_task_specs(
    value: Sequence[Mapping[str, Any]], *, obligation_ids: set[str]
) -> tuple[Mapping[str, Any], ...]:
    specs = _mapping_tuple(value, "plan_revision_task_specs_invalid")
    if not specs:
        raise PlanAuthorityContractError("plan_revision_task_specs_invalid")
    expected = {
        "task_key",
        "capability_id",
        "normalized_input_refs",
        "dependency_task_keys",
        "obligation_edges",
        "execution_rank",
        "declared_budget_units",
        "governor_inputs",
        "execution_policy",
    }
    normalized: list[Mapping[str, Any]] = []
    keys: set[str] = set()
    for spec in specs:
        if set(spec) != expected:
            raise PlanAuthorityContractError("plan_revision_task_specs_invalid")
        task_key = _required_string(
            spec.get("task_key"), "plan_revision_task_specs_invalid"
        )
        if task_key in keys:
            raise PlanAuthorityContractError("plan_revision_task_key_duplicated")
        keys.add(task_key)
        edges = _mapping_tuple(
            spec.get("obligation_edges"), "plan_revision_task_specs_invalid"
        )
        for edge in edges:
            if (
                set(edge) != {"obligation_id", "required"}
                or edge.get("obligation_id") not in obligation_ids
                or type(edge.get("required")) is not bool
            ):
                raise PlanAuthorityContractError(
                    "plan_revision_task_obligation_ref_invalid"
                )
        policy = _execution_policy(
            spec.get("execution_policy"), "plan_revision_task_policy_invalid"
        )
        execution_rank = _positive_integer(
            spec.get("execution_rank"),
            "plan_revision_task_execution_rank_invalid",
        )
        declared_budget_units = _positive_integer(
            spec.get("declared_budget_units"),
            "plan_revision_task_budget_units_invalid",
        )
        governor_inputs = _governor_inputs(
            spec.get("governor_inputs"),
            "plan_revision_task_governor_inputs_invalid",
        )
        normalized.append(
            _freeze(
                {
                    "task_key": task_key,
                    "capability_id": _required_string(
                        spec.get("capability_id"),
                        "plan_revision_task_specs_invalid",
                    ),
                    "normalized_input_refs": _string_tuple(
                        spec.get("normalized_input_refs"),
                        "plan_revision_task_specs_invalid",
                        allow_empty=False,
                    ),
                    "dependency_task_keys": _string_tuple(
                        spec.get("dependency_task_keys"),
                        "plan_revision_task_specs_invalid",
                    ),
                    "obligation_edges": edges,
                    "execution_rank": execution_rank,
                    "declared_budget_units": declared_budget_units,
                    "governor_inputs": governor_inputs,
                    "execution_policy": policy,
                }
            )
        )
    for spec in normalized:
        if any(key not in keys for key in spec["dependency_task_keys"]):
            raise PlanAuthorityContractError("plan_revision_task_dependency_unknown")
        if spec["task_key"] in spec["dependency_task_keys"]:
            raise PlanAuthorityContractError("plan_revision_task_dependency_self")
    ranks = tuple(int(spec["execution_rank"]) for spec in normalized)
    if len(ranks) != len(set(ranks)):
        raise PlanAuthorityContractError("plan_revision_task_execution_rank_duplicated")
    return tuple(normalized)


def _validate_context_window_task_refs(
    context_specs: Sequence[PlanContextWindowSpec],
    task_specs: Sequence[Mapping[str, Any]],
) -> None:
    task_capabilities = {str(task["capability_id"]) for task in task_specs}
    spec_by_capability = {spec.capability_id: spec for spec in context_specs}
    if set(spec_by_capability) - task_capabilities:
        raise PlanAuthorityContractError("plan_revision_context_window_task_missing")
    known_refs = {spec.normalized_input_ref for spec in context_specs}
    for task in task_specs:
        capability_id = str(task["capability_id"])
        input_refs = set(task["normalized_input_refs"])
        actual_refs = {
            ref for ref in input_refs if ref.startswith(_CONTEXT_WINDOW_SPEC_REF_PREFIX)
        }
        spec = spec_by_capability.get(capability_id)
        expected_refs = {spec.normalized_input_ref} if spec is not None else set()
        if actual_refs != expected_refs or actual_refs - known_refs:
            raise PlanAuthorityContractError(
                "plan_revision_context_window_task_ref_mismatch"
            )


def _validate_task_dag(tasks: Sequence[CapabilityTask]) -> None:
    remaining = {task.task_id: set(task.dependency_task_ids) for task in tasks}
    known = set(remaining)
    if any(dependencies - known for dependencies in remaining.values()):
        raise PlanAuthorityContractError("plan_revision_task_dependency_unknown")
    completed: set[str] = set()
    while remaining:
        ready = {
            task_id
            for task_id, dependencies in remaining.items()
            if dependencies <= completed
        }
        if not ready:
            raise PlanAuthorityContractError("plan_revision_task_dependency_cycle")
        completed.update(ready)
        for task_id in ready:
            del remaining[task_id]


@dataclass(frozen=True)
class PlanCompileResult:
    proposal_admission: ProposalAdmissionRecord
    plan_revision: PlanRevision
