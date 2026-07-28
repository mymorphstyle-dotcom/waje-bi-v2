from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.event_window_derivation import (
    EventWindowDerivationError,
    validate_event_window_derivation_policy,
)
from bi_agent.runtime.plan_authority import PlanRevision, PlannerProposal
from bi_agent.runtime.temporal_comparison import (
    EffectiveTemporalComparison,
    temporal_execution_mode,
)


QUERY_BUNDLE_SCHEMA_VERSION = "query-bundle.v1"
QUERY_IR_STATUSES = frozenset(
    {"ready", "repaired", "degraded", "evidenced"}
)
QUERY_PROJECTION_STATUSES = frozenset(
    {"pending", "querying", "evidenced", "limited"}
)


class QueryIRContractError(ValueError):
    pass


QUERY_INPUT_ROUTE_STATUSES = frozenset(
    {"direct", "derived_observation_frame", "unavailable"}
)

_CALENDAR_PARTITION_PATTERN_MODES = {
    "month_phase": "intra_period",
    "iso_weekday": "weekly",
}
_DAILY_CONTEXT_PAYLOAD_ROLES = {
    "cross_source_association": ("reference",),
    "cross_source_panel_association": ("reference",),
    "change_point_scan": ("reference",),
    "outlier_scan": ("target", "reference"),
}
_EVALUATION_RANGE_PAYLOADS = frozenset(
    {
        "data_quality",
        "event_evidence",
        "market_channel_context",
        "metric_coverage_profile",
        "metric_timeseries",
        "source_reconciliation",
    }
)
_PAIR_PAYLOADS = frozenset(
    {
        "candidate_dimension_screen",
        "dimension_distribution",
        "event_window_metric_comparison",
        "formula_graph",
        "funnel_decomposition",
        "high_value_user_contribution",
        "joint_attribution",
        "outlier_contribution",
        "payment_outcome_comparison",
        "segment_contribution",
        "user_mix_contribution",
        "window_metric_comparison",
    }
)


def compile_capability_query_route(
    *,
    capability_id: str,
    capability_contract: Mapping[str, Any],
    temporal_authority: EffectiveTemporalComparison,
) -> dict[str, Any]:
    """Translate business time semantics into one capability input route."""

    capability_id = _required_string(
        capability_id,
        "query_input_route_capability_id_invalid",
    )
    if not isinstance(capability_contract, Mapping) or not isinstance(
        temporal_authority,
        EffectiveTemporalComparison,
    ):
        raise QueryIRContractError("query_input_route_authority_invalid")
    binding = capability_contract.get("task_input_binding")
    payload_kind = (
        str(binding.get("payload_kind") or "")
        if isinstance(binding, Mapping)
        else ""
    )
    execution_mode = temporal_execution_mode(temporal_authority)
    window_roles: tuple[str, ...] = ()
    if (
        temporal_authority.mode == "unresolved"
        or not temporal_authority.has_physical_target
    ):
        status = "unavailable"
        adapter_kind = "none"
        boundary_code = "temporal_authority_unresolved"
    elif not payload_kind:
        status = "unavailable"
        adapter_kind = "none"
        boundary_code = "query_input_binding_missing"
    elif temporal_authority.mode == "calendar_partition":
        partition_field = str(
            (temporal_authority.calendar_partition or {}).get(
                "partition_field"
            )
            or ""
        )
        pattern_mode = (
            str(binding.get("pattern_mode") or "")
            if isinstance(binding, Mapping)
            else ""
        )
        expected_pattern_mode = _CALENDAR_PARTITION_PATTERN_MODES.get(
            partition_field
        )
        if pattern_mode == "rolling":
            if _daily_observation_frame_fits(
                capability_contract,
                temporal_authority=temporal_authority,
            ):
                status = "derived_observation_frame"
                adapter_kind = "daily_observation_frame"
                window_roles = ("target", "reference")
                boundary_code = None
            else:
                status = "unavailable"
                adapter_kind = "none"
                boundary_code = "daily_observation_frame_out_of_bounds"
        elif (
            payload_kind == "pattern"
            and expected_pattern_mode is not None
            and pattern_mode == expected_pattern_mode
        ):
            status = "direct"
            adapter_kind = "partition_member_frame"
            window_roles = ("target",)
            boundary_code = None
        elif payload_kind in _DAILY_CONTEXT_PAYLOAD_ROLES:
            if _daily_observation_frame_fits(
                capability_contract,
                temporal_authority=temporal_authority,
            ):
                status = "derived_observation_frame"
                adapter_kind = "daily_observation_frame"
                window_roles = _DAILY_CONTEXT_PAYLOAD_ROLES[payload_kind]
                boundary_code = None
            else:
                status = "unavailable"
                adapter_kind = "none"
                boundary_code = "daily_observation_frame_out_of_bounds"
        elif payload_kind in _EVALUATION_RANGE_PAYLOADS:
            status = "direct"
            adapter_kind = "evaluation_range_frame"
            window_roles = ("target",)
            boundary_code = None
        elif payload_kind == "event_window_metric_comparison":
            try:
                policy = validate_event_window_derivation_policy(
                    capability_contract.get("dynamic_event_window_policy"),
                )
            except EventWindowDerivationError:
                policy = None
            if (
                isinstance(policy, Mapping)
                and temporal_authority.mode
                in set(policy["eligible_parent_modes"])
            ):
                status = "derived_observation_frame"
                adapter_kind = "event_evidence_join_frame"
                window_roles = ("target",)
                boundary_code = None
            else:
                status = "unavailable"
                adapter_kind = "none"
                boundary_code = "dynamic_event_window_policy_missing"
        elif (
            payload_kind in _PAIR_PAYLOADS
        ):
            status = "derived_observation_frame"
            adapter_kind = "partition_role_frame"
            window_roles = ("target",)
            boundary_code = None
        else:
            status = "unavailable"
            adapter_kind = "none"
            boundary_code = "calendar_partition_adapter_missing"
    else:
        pattern_mode = (
            str(binding.get("pattern_mode") or "")
            if isinstance(binding, Mapping)
            else ""
        )
        if pattern_mode == "rolling":
            status = "direct"
            adapter_kind = "capability_context_frame"
            window_roles = ("target", "reference")
            boundary_code = None
        elif payload_kind == "pattern" and pattern_mode in {
            "intra_period",
            "weekly",
        }:
            status = "unavailable"
            adapter_kind = "none"
            boundary_code = "partition_authority_required"
        elif (
            payload_kind == "event_window_metric_comparison"
            and temporal_authority.mode != "event_relative"
        ):
            status = "unavailable"
            adapter_kind = "none"
            boundary_code = "event_relative_authority_required"
        elif (
            payload_kind in _PAIR_PAYLOADS
            and temporal_authority.baseline_window is None
        ):
            status = "unavailable"
            adapter_kind = "none"
            boundary_code = "baseline_window_required"
        elif payload_kind in _DAILY_CONTEXT_PAYLOAD_ROLES:
            status = "direct"
            adapter_kind = "capability_context_frame"
            window_roles = _DAILY_CONTEXT_PAYLOAD_ROLES[payload_kind]
            boundary_code = None
        else:
            status = "direct"
            adapter_kind = "physical_window_frame"
            window_roles = (
                ("target", "baseline")
                if temporal_authority.baseline_window is not None
                else ("target",)
            )
            boundary_code = None
    route = {
        "schema_version": "capability-query-route.v2",
        "capability_id": capability_id,
        "status": status,
        "adapter_kind": adapter_kind,
        "payload_kind": payload_kind,
        "temporal_execution_mode": execution_mode,
        "observation_grain": (
            "day"
            if adapter_kind
            in {
                "capability_context_frame",
                "daily_observation_frame",
                "evaluation_range_frame",
                "event_evidence_join_frame",
                "partition_member_frame",
                "partition_role_frame",
            }
            or temporal_authority.mode == "calendar_partition"
            else "window"
        ),
        "window_roles": list(window_roles),
        "partition_field": (
            str(
                (temporal_authority.calendar_partition or {}).get(
                    "partition_field"
                )
                or ""
            )
            if temporal_authority.mode == "calendar_partition"
            else ""
        ),
        "partition_frame": (
            canonical_value(temporal_authority.calendar_partition)
            if temporal_authority.mode == "calendar_partition"
            else None
        ),
        "boundary_code": boundary_code,
    }
    return {
        **route,
        "route_ref": f"capability-query-route:{adapter_kind}:sha256:"
        + canonical_digest(route),
    }


def _daily_observation_frame_fits(
    capability_contract: Mapping[str, Any],
    *,
    temporal_authority: EffectiveTemporalComparison,
) -> bool:
    policy = capability_contract.get("context_window_policy")
    target_window = temporal_authority.target_window
    if (
        not isinstance(policy, Mapping)
        or policy.get("aggregation") != "daily_observations"
        or target_window.start is None
        or target_window.end is None
    ):
        return False
    bounds = policy.get("count_bounds")
    day_bounds = bounds.get("day") if isinstance(bounds, Mapping) else None
    if (
        not isinstance(day_bounds, list)
        or len(day_bounds) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in day_bounds
        )
    ):
        return False
    try:
        observation_days = (
            date.fromisoformat(target_window.end)
            - date.fromisoformat(target_window.start)
        ).days + 1
    except ValueError:
        return False
    return day_bounds[0] <= observation_days <= day_bounds[1]


def compile_task_query_routes(
    *,
    planner_proposal: PlannerProposal,
    analysis_axes: Sequence[Any],
    temporal_authority: EffectiveTemporalComparison,
    runtime_registry: Any,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Compile issue-aware query input routes before concrete tasks exist."""

    if not isinstance(planner_proposal, PlannerProposal):
        raise QueryIRContractError("query_route_planner_proposal_invalid")
    issue_claims = {
        str(issue["issue_id"]): str(issue["target_claim_kind"])
        for issue in planner_proposal.issue_tree
    }
    planner_axis_claims = _planner_axis_supported_claims(planner_proposal)
    planner_issue_ids_by_axis = _planner_issue_ids_by_axis(planner_proposal)
    root_issue_id = str(planner_proposal.issue_tree[0]["issue_id"])
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for axis in analysis_axes:
        axis_id = _required_string(
            getattr(axis, "axis_id", None),
            "query_route_axis_invalid",
        )
        for capability_id in getattr(axis, "capability_refs", ()):
            contract = runtime_registry.capability_inputs(capability_id)
            route = compile_capability_query_route(
                capability_id=str(capability_id),
                capability_contract=contract,
                temporal_authority=temporal_authority,
            )
            supported_claims = set(contract.get("supported_claim_types") or ())
            supported_claims.update(planner_axis_claims.get(axis_id, ()))
            matched_issue_ids = tuple(
                dict.fromkeys(
                    (
                        *planner_issue_ids_by_axis.get(axis_id, ()),
                        *(
                            issue_id
                            for issue_id, claim_kind in issue_claims.items()
                            if claim_kind in supported_claims
                        ),
                    )
                )
            )
            if not matched_issue_ids:
                matched_issue_ids = (root_issue_id,)
            bound_route = {
                **route,
                "analysis_axis_id": axis_id,
                "issue_ids": matched_issue_ids,
            }
            bound_route.pop("route_ref", None)
            routes[(axis_id, str(capability_id))] = {
                **bound_route,
                "route_ref": (
                    "capability-query-route:"
                    + str(bound_route["adapter_kind"])
                    + ":sha256:"
                    + canonical_digest(bound_route)
                ),
            }
    return routes


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise QueryIRContractError(error)
    return value


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _string_tuple(value: Any, error: str) -> tuple[str, ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in value
        )
    ):
        raise QueryIRContractError(error)
    normalized = tuple(dict.fromkeys(value))
    if len(normalized) != len(value):
        raise QueryIRContractError(error)
    return normalized


def _mapping_tuple(value: Any, error: str) -> tuple[Mapping[str, Any], ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or any(not isinstance(item, Mapping) for item in value)
    ):
        raise QueryIRContractError(error)
    return tuple(canonical_value(item) for item in value)


@dataclass(frozen=True)
class QueryIR:
    query_ir_ref: str
    issue_id: str
    parent_issue_id: str | None
    question: str
    target_claim_kind: str
    status: str
    task_ids: tuple[str, ...]
    capability_ids: tuple[str, ...]
    metric_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    context_source_refs: tuple[str, ...]
    aggregation_grain: str
    observation_frame_refs: tuple[str, ...]
    dependency_task_ids: tuple[str, ...]
    query_slices: tuple[Mapping[str, Any], ...]
    repair_records: tuple[Mapping[str, Any], ...]
    boundary_code: str | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        issue_id: str,
        parent_issue_id: str | None,
        question: str,
        target_claim_kind: str,
        status: str,
        task_ids: Sequence[str],
        capability_ids: Sequence[str],
        metric_refs: Sequence[str],
        dimension_refs: Sequence[str],
        context_source_refs: Sequence[str],
        aggregation_grain: str,
        observation_frame_refs: Sequence[str],
        dependency_task_ids: Sequence[str],
        query_slices: Sequence[Mapping[str, Any]],
        repair_records: Sequence[Mapping[str, Any]],
        boundary_code: str | None,
    ) -> "QueryIR":
        issue_id = _required_string(issue_id, "query_ir_issue_id_invalid")
        parent_issue_id = _optional_string(
            parent_issue_id, "query_ir_parent_issue_id_invalid"
        )
        question = _required_string(question, "query_ir_question_invalid")
        target_claim_kind = _required_string(
            target_claim_kind, "query_ir_claim_kind_invalid"
        )
        if status not in QUERY_IR_STATUSES:
            raise QueryIRContractError("query_ir_status_invalid")
        tasks = _string_tuple(task_ids, "query_ir_task_ids_invalid")
        capabilities = _string_tuple(
            capability_ids, "query_ir_capability_ids_invalid"
        )
        metrics = _string_tuple(metric_refs, "query_ir_metric_refs_invalid")
        dimensions = _string_tuple(
            dimension_refs, "query_ir_dimension_refs_invalid"
        )
        context_sources = _string_tuple(
            context_source_refs, "query_ir_context_source_refs_invalid"
        )
        aggregation_grain = _required_string(
            aggregation_grain, "query_ir_aggregation_grain_invalid"
        )
        frame_refs = _string_tuple(
            observation_frame_refs, "query_ir_observation_frame_refs_invalid"
        )
        dependencies = _string_tuple(
            dependency_task_ids, "query_ir_dependency_task_ids_invalid"
        )
        slices = _mapping_tuple(query_slices, "query_ir_query_slices_invalid")
        repairs = _mapping_tuple(repair_records, "query_ir_repair_records_invalid")
        boundary_code = _optional_string(
            boundary_code, "query_ir_boundary_code_invalid"
        )
        if status == "degraded" and boundary_code is None:
            raise QueryIRContractError("query_ir_boundary_required")
        if status != "degraded" and not tasks:
            raise QueryIRContractError("query_ir_executable_task_required")
        if status == "evidenced":
            if boundary_code is not None:
                raise QueryIRContractError("query_ir_evidenced_boundary_invalid")
        body = {
            "issue_id": issue_id,
            "parent_issue_id": parent_issue_id,
            "question": question,
            "target_claim_kind": target_claim_kind,
            "status": status,
            "task_ids": tasks,
            "capability_ids": capabilities,
            "metric_refs": metrics,
            "dimension_refs": dimensions,
            "context_source_refs": context_sources,
            "aggregation_grain": aggregation_grain,
            "observation_frame_refs": frame_refs,
            "dependency_task_ids": dependencies,
            "query_slices": slices,
            "repair_records": repairs,
            "boundary_code": boundary_code,
        }
        digest = canonical_digest(body)
        return cls(
            query_ir_ref="query-ir-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QueryIR":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise QueryIRContractError("query_ir_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in fields
                if key not in {"query_ir_ref", "content_digest"}
            }
        )
        if (
            rebuilt.query_ir_ref != payload.get("query_ir_ref")
            or rebuilt.content_digest != payload.get("content_digest")
        ):
            raise QueryIRContractError("query_ir_identity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self.__dict__)


@dataclass(frozen=True)
class QueryBundle:
    query_bundle_ref: str
    schema_version: str
    stage: str
    source_query_bundle_ref: str | None
    run_attempt_id: str
    intent_revision_id: str
    plan_revision_id: str
    planner_proposal_id: str
    aggregation_grain: str
    query_nodes: tuple[QueryIR, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        stage: str,
        source_query_bundle_ref: str | None,
        run_attempt_id: str,
        intent_revision_id: str,
        plan_revision_id: str,
        planner_proposal_id: str,
        aggregation_grain: str,
        query_nodes: Sequence[QueryIR | Mapping[str, Any]],
    ) -> "QueryBundle":
        if stage not in {"compiled", "settled"}:
            raise QueryIRContractError("query_bundle_stage_invalid")
        source_query_bundle_ref = _optional_string(
            source_query_bundle_ref,
            "query_bundle_source_ref_invalid",
        )
        if (stage == "compiled") != (source_query_bundle_ref is None):
            raise QueryIRContractError("query_bundle_source_ref_invalid")
        nodes = tuple(
            item if isinstance(item, QueryIR) else QueryIR.from_dict(item)
            for item in query_nodes
        )
        if not nodes or len({item.issue_id for item in nodes}) != len(nodes):
            raise QueryIRContractError("query_bundle_nodes_invalid")
        body = {
            "schema_version": QUERY_BUNDLE_SCHEMA_VERSION,
            "stage": stage,
            "source_query_bundle_ref": source_query_bundle_ref,
            "run_attempt_id": _required_string(
                run_attempt_id, "query_bundle_run_id_invalid"
            ),
            "intent_revision_id": _required_string(
                intent_revision_id, "query_bundle_intent_id_invalid"
            ),
            "plan_revision_id": _required_string(
                plan_revision_id, "query_bundle_plan_id_invalid"
            ),
            "planner_proposal_id": _required_string(
                planner_proposal_id, "query_bundle_proposal_id_invalid"
            ),
            "aggregation_grain": _required_string(
                aggregation_grain, "query_bundle_grain_invalid"
            ),
            "query_nodes": nodes,
        }
        digest = canonical_digest(
            {
                **body,
                "query_nodes": tuple(item.to_dict() for item in nodes),
            }
        )
        return cls(
            query_bundle_ref="query-bundle-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QueryBundle":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise QueryIRContractError("query_bundle_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in fields
                if key
                not in {
                    "query_bundle_ref",
                    "schema_version",
                    "content_digest",
                }
            }
        )
        if (
            payload.get("schema_version") != QUERY_BUNDLE_SCHEMA_VERSION
            or rebuilt.query_bundle_ref != payload.get("query_bundle_ref")
            or rebuilt.content_digest != payload.get("content_digest")
        ):
            raise QueryIRContractError("query_bundle_identity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(
            {
                "query_bundle_ref": self.query_bundle_ref,
                "schema_version": self.schema_version,
                "stage": self.stage,
                "source_query_bundle_ref": self.source_query_bundle_ref,
                "run_attempt_id": self.run_attempt_id,
                "intent_revision_id": self.intent_revision_id,
                "plan_revision_id": self.plan_revision_id,
                "planner_proposal_id": self.planner_proposal_id,
                "aggregation_grain": self.aggregation_grain,
                "query_nodes": tuple(
                    item.to_dict() for item in self.query_nodes
                ),
                "content_digest": self.content_digest,
            }
        )

    def customer_projection(self) -> dict[str, Any]:
        return {
            "schema_version": "query-bundle-projection.v1",
            "query_bundle_ref": self.query_bundle_ref,
            "stage": self.stage,
            "plan_revision_id": self.plan_revision_id,
            "planner_proposal_id": self.planner_proposal_id,
            "issues": [
                {
                    "issue_id": node.issue_id,
                    "question": node.question,
                    "status": _projection_status(node.status),
                    "status_message": _projection_status_message(node.status),
                    "query_ir_ref": node.query_ir_ref,
                    "repair_actions": [
                        str(item["action"])
                        for item in node.repair_records
                        if isinstance(item.get("action"), str)
                    ],
                }
                for node in self.query_nodes
            ],
        }


def compile_query_bundle(
    *,
    plan_revision: PlanRevision,
    planner_proposal: PlannerProposal,
    runtime_registry: Any,
) -> QueryBundle:
    if not isinstance(plan_revision, PlanRevision) or not isinstance(
        planner_proposal, PlannerProposal
    ):
        raise QueryIRContractError("query_bundle_authority_invalid")
    if (
        plan_revision.run_attempt_id != planner_proposal.run_attempt_id
        or plan_revision.intent_revision_id != planner_proposal.intent_revision_id
        or plan_revision.planner_proposal_ref
        != planner_proposal.planner_proposal_id
        or plan_revision.authority_context_ref
        != planner_proposal.authority_context_ref
    ):
        raise QueryIRContractError("query_bundle_authority_mismatch")

    obligation_by_id = {
        item.obligation_id: item for item in plan_revision.claim_obligations
    }
    task_by_id = {item.task_id: item for item in plan_revision.capability_tasks}
    issue_ids_by_axis = _planner_issue_ids_by_axis(planner_proposal)
    direct_nodes: list[QueryIR] = []
    for issue in planner_proposal.issue_tree:
        direct_nodes.append(
            _compile_issue_query_ir(
                issue=issue,
                plan_revision=plan_revision,
                obligation_by_id=obligation_by_id,
                issue_ids_by_axis=issue_ids_by_axis,
                runtime_registry=runtime_registry,
            )
        )

    parent_by_issue = {
        str(item["issue_id"]): (
            str(item["parent_issue_id"])
            if item["parent_issue_id"] is not None
            else None
        )
        for item in planner_proposal.issue_tree
    }
    descendants_by_parent: dict[str, list[QueryIR]] = {}
    for node in direct_nodes:
        ancestor = node.parent_issue_id
        while ancestor is not None:
            descendants_by_parent.setdefault(ancestor, []).append(node)
            ancestor = parent_by_issue.get(ancestor)

    ordered: list[QueryIR] = []
    direct_by_id = {item.issue_id: item for item in direct_nodes}
    for issue in planner_proposal.issue_tree:
        issue_id = str(issue["issue_id"])
        direct_node = direct_by_id[issue_id]
        descendants = descendants_by_parent.get(issue_id, [])
        if not descendants:
            ordered.append(direct_node)
            continue
        task_ids = tuple(
            dict.fromkeys(
                (
                    *direct_node.task_ids,
                    *(
                        task_id
                        for node in descendants
                        for task_id in node.task_ids
                    ),
                )
            )
        )
        tasks = tuple(task_by_id[item] for item in task_ids)
        repair_records = tuple(
            (
                *direct_node.repair_records,
                *(
                    (
                        {
                            "action": "compose_child_queries",
                            "reason": "parent_issue_combines_own_and_child_query_routes",
                            "preserves_business_semantics": True,
                        },
                    )
                    if any(node.task_ids for node in descendants)
                    else ()
                ),
            )
        )
        status = "repaired" if task_ids and repair_records else (
            "ready" if task_ids else "degraded"
        )
        ordered.append(
            QueryIR.create(
                issue_id=issue_id,
                parent_issue_id=issue["parent_issue_id"],
                question=str(issue["question"]),
                target_claim_kind=str(issue["target_claim_kind"]),
                status=status,
                task_ids=task_ids,
                capability_ids=tuple(
                    dict.fromkeys(item.capability_id for item in tasks)
                ),
                metric_refs=_input_refs(tasks, "metric:"),
                dimension_refs=_supported_dimension_refs(
                    tasks, runtime_registry, aggregation_grain="window_id"
                ),
                context_source_refs=_input_refs(tasks, "context-source:"),
                aggregation_grain="window_id",
                observation_frame_refs=_observation_frame_refs(
                    tasks, plan_revision
                ),
                dependency_task_ids=tuple(
                    dict.fromkeys(
                        dependency
                        for task in tasks
                        for dependency in task.dependency_task_ids
                    )
                ),
                query_slices=tuple(
                    _query_slice(task, plan_revision) for task in tasks
                ),
                repair_records=repair_records,
                boundary_code=(
                    None if tasks else "no_contract_admissible_query_route"
                ),
            )
        )
    if {item.issue_id for item in ordered} != {
        str(item["issue_id"]) for item in planner_proposal.issue_tree
    }:
        raise QueryIRContractError("query_bundle_issue_closure_invalid")
    planned_task_ids = set(task_by_id)
    projected_task_ids = {
        task_id for node in ordered for task_id in node.task_ids
    }
    if projected_task_ids != planned_task_ids:
        missing_task_ids = sorted(
            f"{task_by_id[task_id].task_key}={task_id}"
            for task_id in planned_task_ids - projected_task_ids
        )
        unexpected_task_ids = sorted(projected_task_ids - planned_task_ids)
        raise QueryIRContractError(
            "query_bundle_task_closure_invalid:"
            + ",".join(missing_task_ids)
            + ":unexpected:"
            + ",".join(unexpected_task_ids)
        )
    return QueryBundle.create(
        stage="compiled",
        source_query_bundle_ref=None,
        run_attempt_id=plan_revision.run_attempt_id,
        intent_revision_id=plan_revision.intent_revision_id,
        plan_revision_id=plan_revision.plan_revision_id,
        planner_proposal_id=planner_proposal.planner_proposal_id,
        aggregation_grain="window_id",
        query_nodes=ordered,
    )


def settle_query_bundle(
    query_bundle: QueryBundle,
    capability_outcome_bundles: Sequence[Any],
) -> QueryBundle:
    if not isinstance(query_bundle, QueryBundle) or query_bundle.stage != "compiled":
        raise QueryIRContractError("query_bundle_settlement_input_invalid")
    outcomes = {
        bundle[1].task_id: bundle[1]
        for bundle in capability_outcome_bundles
        if isinstance(bundle, tuple) and len(bundle) == 4
    }
    settled: list[QueryIR] = []
    for node in query_bundle.query_nodes:
        matched = tuple(
            outcomes[task_id] for task_id in node.task_ids if task_id in outcomes
        )
        evidenced = tuple(
            item
            for item in matched
            if item.status == "succeeded" and item.evidence_refs
        )
        if evidenced:
            status = "evidenced"
            boundary_code = None
        elif node.status == "degraded":
            status = "degraded"
            boundary_code = node.boundary_code
        elif len(matched) < len(node.task_ids):
            status = "degraded"
            boundary_code = "query_route_not_settled"
        else:
            status = "degraded"
            boundary_code = "query_route_settled_with_boundary"
        settled.append(
            QueryIR.create(
                issue_id=node.issue_id,
                parent_issue_id=node.parent_issue_id,
                question=node.question,
                target_claim_kind=node.target_claim_kind,
                status=status,
                task_ids=node.task_ids,
                capability_ids=node.capability_ids,
                metric_refs=node.metric_refs,
                dimension_refs=node.dimension_refs,
                context_source_refs=node.context_source_refs,
                aggregation_grain=node.aggregation_grain,
                observation_frame_refs=node.observation_frame_refs,
                dependency_task_ids=node.dependency_task_ids,
                query_slices=node.query_slices,
                repair_records=node.repair_records,
                boundary_code=boundary_code,
            )
        )
    return QueryBundle.create(
        stage="settled",
        source_query_bundle_ref=query_bundle.query_bundle_ref,
        run_attempt_id=query_bundle.run_attempt_id,
        intent_revision_id=query_bundle.intent_revision_id,
        plan_revision_id=query_bundle.plan_revision_id,
        planner_proposal_id=query_bundle.planner_proposal_id,
        aggregation_grain=query_bundle.aggregation_grain,
        query_nodes=settled,
    )


def _compile_issue_query_ir(
    *,
    issue: Mapping[str, Any],
    plan_revision: PlanRevision,
    obligation_by_id: Mapping[str, Any],
    issue_ids_by_axis: Mapping[str, tuple[str, ...]],
    runtime_registry: Any,
) -> QueryIR:
    issue_id = str(issue["issue_id"])
    claim_kind = str(issue["target_claim_kind"])
    exact_obligation_ids = {
        obligation_id
        for obligation_id, obligation in obligation_by_id.items()
        if obligation.claim_kind == claim_kind
    }
    exact_tasks = tuple(
        task
        for task in plan_revision.capability_tasks
        if exact_obligation_ids.intersection(task.supports_obligation_ids)
    )
    planner_axis_tasks = tuple(
        task
        for task in plan_revision.capability_tasks
        if issue_id in issue_ids_by_axis.get(_task_axis_id(task), ())
        or (
            issue["parent_issue_id"] is None
            and not issue_ids_by_axis.get(_task_axis_id(task), ())
        )
    )
    contract_tasks = tuple(
        task
        for task in plan_revision.capability_tasks
        if claim_kind
        in set(
            runtime_registry.capability_inputs(task.capability_id).get(
                "supported_claim_types", ()
            )
        )
    )
    repairs: list[Mapping[str, Any]] = []
    candidates = _distinct_tasks(
        (*exact_tasks, *planner_axis_tasks, *contract_tasks)
    )
    if contract_tasks and not exact_tasks:
        repairs.append(
            {
                "action": "bind_contract_supported_claim_route",
                "reason": "planner_claim_has_no_direct_obligation_edge",
                "preserves_business_semantics": True,
            }
        )
    available = tuple(task for task in candidates if _task_inputs_queryable(task))
    if len(available) < len(candidates):
        repairs.append(
            {
                "action": "preserve_degraded_task_route",
                "reason": "planned_task_remains_owned_when_input_has_a_boundary",
                "preserves_business_semantics": True,
            }
        )
    tasks = tuple(
        sorted(candidates, key=lambda item: (item.execution_rank, item.task_id))
    )
    if len({item.capability_id for item in tasks}) > 1:
        repairs.append(
            {
                "action": "split_by_capability",
                "reason": "issue_requires_complementary_query_families",
                "preserves_business_semantics": True,
            }
        )
    frame_refs = _observation_frame_refs(tasks, plan_revision)
    if frame_refs:
        repairs.append(
            {
                "action": "derive_observation_frame",
                "reason": "capability_uses_a_different_observation_shape",
                "preserves_business_semantics": True,
            }
        )
    status = "repaired" if repairs else "ready"
    boundary_code = None
    if not tasks:
        status = "degraded"
        boundary_code = "no_contract_admissible_query_route"
    return QueryIR.create(
        issue_id=issue_id,
        parent_issue_id=issue["parent_issue_id"],
        question=str(issue["question"]),
        target_claim_kind=claim_kind,
        status=status,
        task_ids=tuple(item.task_id for item in tasks),
        capability_ids=tuple(dict.fromkeys(item.capability_id for item in tasks)),
        metric_refs=_input_refs(tasks, "metric:"),
        dimension_refs=_supported_dimension_refs(
            tasks, runtime_registry, aggregation_grain="window_id"
        ),
        context_source_refs=_input_refs(tasks, "context-source:"),
        aggregation_grain="window_id",
        observation_frame_refs=frame_refs,
        dependency_task_ids=tuple(
            dict.fromkeys(
                dependency
                for task in tasks
                for dependency in task.dependency_task_ids
            )
        ),
        query_slices=tuple(_query_slice(task, plan_revision) for task in tasks),
        repair_records=tuple(repairs),
        boundary_code=boundary_code,
    )


def _planner_axis_supported_claims(
    planner_proposal: PlannerProposal,
) -> dict[str, tuple[str, ...]]:
    claims_by_axis: dict[str, list[str]] = {}
    for item in planner_proposal.auxiliary_axes:
        axis_id = str(item["axis_id"])
        claims = claims_by_axis.setdefault(axis_id, [])
        claims.extend(str(value) for value in item["supports_claim_kinds"])
    return {
        axis_id: tuple(dict.fromkeys(claims))
        for axis_id, claims in claims_by_axis.items()
    }


def _planner_issue_ids_by_axis(
    planner_proposal: PlannerProposal,
) -> dict[str, tuple[str, ...]]:
    issue_ids_by_claim: dict[str, list[str]] = {}
    for issue in planner_proposal.issue_tree:
        issue_ids_by_claim.setdefault(
            str(issue["target_claim_kind"]),
            [],
        ).append(str(issue["issue_id"]))
    issue_ids_by_axis: dict[str, list[str]] = {}
    for axis_id, claim_kinds in _planner_axis_supported_claims(
        planner_proposal
    ).items():
        issue_ids = issue_ids_by_axis.setdefault(axis_id, [])
        for claim_kind in claim_kinds:
            for issue_id in issue_ids_by_claim.get(claim_kind, ()):
                if issue_id not in issue_ids:
                    issue_ids.append(issue_id)
    for hypothesis in planner_proposal.hypotheses:
        issue_ref = str(hypothesis["issue_ref"])
        for axis_id in hypothesis["requested_axis_ids"]:
            issue_ids = issue_ids_by_axis.setdefault(str(axis_id), [])
            if issue_ref not in issue_ids:
                issue_ids.append(issue_ref)
    return {
        axis_id: tuple(issue_ids)
        for axis_id, issue_ids in issue_ids_by_axis.items()
    }


def _task_axis_id(task: Any) -> str:
    task_key = _required_string(
        getattr(task, "task_key", None),
        "query_route_task_key_invalid",
    )
    axis_id, separator, _ = task_key.partition(":")
    if not separator:
        raise QueryIRContractError("query_route_task_key_invalid")
    return axis_id


def _distinct_tasks(tasks: Sequence[Any]) -> tuple[Any, ...]:
    distinct: list[Any] = []
    task_ids: set[str] = set()
    for task in tasks:
        task_id = _required_string(
            getattr(task, "task_id", None),
            "query_route_task_id_invalid",
        )
        if task_id in task_ids:
            continue
        task_ids.add(task_id)
        distinct.append(task)
    return tuple(distinct)


def _task_inputs_queryable(task: Any) -> bool:
    states = tuple(task.execution_policy.get("input_states") or ())
    if not states:
        return True
    return any(
        str(item.get("availability") or "")
        in {"available", "claim_ready", "context_only"}
        for item in states
    )


def _input_refs(tasks: Sequence[Any], prefix: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            ref[len(prefix) :]
            for task in tasks
            for ref in task.normalized_input_refs
            if ref.startswith(prefix)
        )
    )


def _supported_dimension_refs(
    tasks: Sequence[Any],
    runtime_registry: Any,
    *,
    aggregation_grain: str,
) -> tuple[str, ...]:
    supported: list[str] = []
    for dimension_ref in _input_refs(tasks, "dimension:"):
        dataset_ids = {
            ref.removeprefix("dataset:")
            for task in tasks
            for ref in task.normalized_input_refs
            if ref.startswith("dataset:")
        }
        if any(
            _dimension_supports_grain(
                runtime_registry,
                dimension_ref=dimension_ref,
                dataset_id=dataset_id,
                aggregation_grain=aggregation_grain,
            )
            for dataset_id in dataset_ids
        ):
            supported.append(dimension_ref)
    return tuple(supported)


def _dimension_supports_grain(
    runtime_registry: Any,
    *,
    dimension_ref: str,
    dataset_id: str,
    aggregation_grain: str,
) -> bool:
    try:
        contract = runtime_registry.dimension(
            dimension_ref,
            dataset_id=dataset_id,
        )
    except KeyError:
        return False
    return aggregation_grain in set(contract.get("allowed_grains", ()))


def _observation_frame_refs(
    tasks: Sequence[Any], plan_revision: PlanRevision
) -> tuple[str, ...]:
    capabilities = {item.capability_id for item in tasks}
    return tuple(
        spec.normalized_input_ref
        for spec in plan_revision.context_window_specs
        if spec.capability_id in capabilities
    )


def _query_slice(task: Any, plan_revision: PlanRevision) -> dict[str, Any]:
    return {
        "slice_ref": f"query-slice:{task.task_id}",
        "task_id": task.task_id,
        "capability_id": task.capability_id,
        "metric_refs": [
            item.removeprefix("metric:")
            for item in task.normalized_input_refs
            if item.startswith("metric:")
        ],
        "dimension_refs": [
            item.removeprefix("dimension:")
            for item in task.normalized_input_refs
            if item.startswith("dimension:")
        ],
        "context_source_refs": [
            item.removeprefix("context-source:")
            for item in task.normalized_input_refs
            if item.startswith("context-source:")
        ],
        "window_refs": list(plan_revision.resolved_window_refs),
        "dependency_task_ids": list(task.dependency_task_ids),
        "query_input_route_refs": [
            item
            for item in task.normalized_input_refs
            if item.startswith("capability-query-route:")
        ],
    }


def _projection_status(status: str) -> str:
    if status == "evidenced":
        return "evidenced"
    if status == "degraded":
        return "limited"
    return "querying"


def _projection_status_message(status: str) -> str:
    return {
        "ready": "查询中",
        "repaired": "已调整路线",
        "degraded": "有边界",
        "evidenced": "已有证据",
    }[status]


__all__ = [
    "QUERY_BUNDLE_SCHEMA_VERSION",
    "QUERY_INPUT_ROUTE_STATUSES",
    "QUERY_IR_STATUSES",
    "QUERY_PROJECTION_STATUSES",
    "QueryBundle",
    "QueryIR",
    "QueryIRContractError",
    "compile_query_bundle",
    "compile_capability_query_route",
    "compile_task_query_routes",
    "settle_query_bundle",
]
