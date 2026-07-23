from __future__ import annotations

from collections import OrderedDict
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_taxonomy import publication_evidence_kinds
from bi_agent.runtime.plan_authority import (
    CAPABILITY_TASK_DECLARED_BUDGET_UNITS,
    AnalysisAxis,
    AuthorityContext,
    ClaimObligation,
    EvidenceRequirement,
    PlanAuthorityContractError,
    PlanCompileResult,
    PlannerProposal,
    PlanContextWindowSpec,
    PlanRevision,
    ProposalAdmissionRecord,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.single_authority import DecisionLedger, IntentRevision
from bi_agent.runtime.temporal_comparison import (
    EffectiveTemporalComparison,
    TemporalComparisonContractError,
    capability_supports_temporal_authority,
    resolve_effective_comparison,
    resolve_rolling_window_strategy,
)


_AXIS_ROLE_PRIORITY = {
    "conditional": 0,
    "auxiliary": 1,
    "disclosure": 2,
    "required": 3,
}


class AuthoritativePlanCompiler:
    """Compile one accepted plan from immutable intent, decisions, and contracts."""

    compiler_version = "single-authority-plan-compiler.v1"

    def __init__(self, *, runtime_registry: RuntimeContractRegistry) -> None:
        if not isinstance(runtime_registry, RuntimeContractRegistry):
            raise PlanAuthorityContractError("plan_compiler_registry_invalid")
        self._registry = runtime_registry

    def compile(
        self,
        *,
        intent_revision: IntentRevision,
        decision_ledger: DecisionLedger,
        authority_context: AuthorityContext,
        planner_proposal: PlannerProposal,
        supersedes_plan_revision: PlanRevision | None = None,
    ) -> PlanCompileResult:
        self._validate_authorities(
            intent_revision=intent_revision,
            decision_ledger=decision_ledger,
            authority_context=authority_context,
            planner_proposal=planner_proposal,
            supersedes_plan_revision=supersedes_plan_revision,
        )
        budget_policy = self._registry.exploration_budget_policy
        active_decisions = decision_ledger.active_records()
        decision_refs = tuple(record.decision_id for record in active_decisions)
        goal_plans = self._compile_goal_plans(intent_revision)
        goal_axes = self._merge_goal_axes(goal_plans)
        temporal_authority = self._effective_temporal_authority(
            intent_revision=intent_revision,
            decision_ledger=decision_ledger,
            goal_axes=goal_axes,
        )
        mandatory_obligations = (
            *self._mandatory_obligations(
                intent_revision=intent_revision,
                goal_plans=goal_plans,
                temporal_authority=temporal_authority,
            ),
            *self._requested_factor_obligations(
                intent_revision=intent_revision,
                temporal_authority=temporal_authority,
            ),
        )
        resolved_window_refs = temporal_authority.resolved_window_refs

        admission_entries, admitted = self._admit_proposal(
            intent_revision=intent_revision,
            authority_context=authority_context,
            planner_proposal=planner_proposal,
            goal_axes=goal_axes,
            mandatory_obligations=mandatory_obligations,
            resolved_window_refs=resolved_window_refs,
        )
        proposal_admission = ProposalAdmissionRecord.create(
            planner_proposal_ref=planner_proposal.planner_proposal_id,
            intent_revision_id=intent_revision.intent_revision_id,
            decision_refs=decision_refs,
            authority_context_ref=authority_context.authority_context_ref,
            admission_entries=admission_entries,
            compiler_version=self.compiler_version,
            contract_versions=authority_context.contract_versions,
        )

        analyst_obligations = self._analyst_obligations(
            intent_revision=intent_revision,
            planner_proposal=planner_proposal,
            admitted=admitted,
        )
        candidate_obligations = (
            *mandatory_obligations,
            *analyst_obligations,
        )
        claim_obligations = (
            candidate_obligations
            if supersedes_plan_revision is None
            else self._preserve_superseded_obligations(
                source_plan=supersedes_plan_revision,
                candidate_obligations=candidate_obligations,
            )
        )
        analysis_axes = self._analysis_axes(
            intent_revision=intent_revision,
            planner_proposal=planner_proposal,
            goal_axes=goal_axes,
            claim_obligations=claim_obligations,
            admitted=admitted,
        )
        task_specs = self._capability_task_specs(
            authority_context=authority_context,
            resolved_window_refs=resolved_window_refs,
            claim_obligations=claim_obligations,
            analysis_axes=analysis_axes,
            temporal_authority=temporal_authority,
        )
        task_specs = self._bind_capability_task_dependencies(task_specs)
        context_window_specs = self._compile_context_window_specs(
            task_specs,
            temporal_authority=temporal_authority,
        )
        task_specs = self._bind_context_window_specs(
            task_specs,
            context_window_specs,
        )
        if supersedes_plan_revision is not None:
            task_specs = self._preserve_superseded_task_ranks(
                source_plan=supersedes_plan_revision,
                candidate_task_specs=task_specs,
            )
        plan_revision = PlanRevision.create(
            run_attempt_id=intent_revision.run_attempt_id,
            supersedes_plan_revision_id=(
                supersedes_plan_revision.plan_revision_id
                if supersedes_plan_revision is not None
                else None
            ),
            intent_revision_id=intent_revision.intent_revision_id,
            decision_refs=decision_refs,
            authority_context_ref=authority_context.authority_context_ref,
            planner_proposal_ref=planner_proposal.planner_proposal_id,
            proposal_admission_ref=proposal_admission.proposal_admission_id,
            temporal_authority=temporal_authority,
            resolved_window_refs=resolved_window_refs,
            context_window_specs=context_window_specs,
            claim_obligations=claim_obligations,
            analysis_axes=analysis_axes,
            capability_task_specs=task_specs,
            assumption_refs=tuple(admitted["assumption_refs"]),
            budget_policy_ref=budget_policy.budget_policy_ref,
            contract_versions=authority_context.contract_versions,
        )
        return PlanCompileResult(
            proposal_admission=proposal_admission,
            plan_revision=plan_revision,
        )

    def _validate_authorities(
        self,
        *,
        intent_revision: IntentRevision,
        decision_ledger: DecisionLedger,
        authority_context: AuthorityContext,
        planner_proposal: PlannerProposal,
        supersedes_plan_revision: PlanRevision | None,
    ) -> None:
        if not isinstance(intent_revision, IntentRevision):
            raise PlanAuthorityContractError("plan_compiler_intent_invalid")
        if not isinstance(decision_ledger, DecisionLedger):
            raise PlanAuthorityContractError("plan_compiler_decision_ledger_invalid")
        if not isinstance(authority_context, AuthorityContext):
            raise PlanAuthorityContractError("plan_compiler_authority_context_invalid")
        if not isinstance(planner_proposal, PlannerProposal):
            raise PlanAuthorityContractError("plan_compiler_proposal_invalid")
        if authority_context.run_attempt_id != intent_revision.run_attempt_id:
            raise PlanAuthorityContractError("plan_compiler_run_authority_mismatch")
        active_decisions = decision_ledger.active_records()
        if any(
            record.intent_revision_id != intent_revision.intent_revision_id
            for record in active_decisions
        ):
            raise PlanAuthorityContractError("plan_compiler_active_decision_stale")
        decision_refs = tuple(record.decision_id for record in active_decisions)
        if (
            planner_proposal.run_attempt_id != intent_revision.run_attempt_id
            or planner_proposal.intent_revision_id != intent_revision.intent_revision_id
            or planner_proposal.authority_context_ref
            != authority_context.authority_context_ref
            or planner_proposal.decision_refs != decision_refs
        ):
            raise PlanAuthorityContractError(
                "plan_compiler_proposal_authority_mismatch"
            )
        for slot in intent_revision.ambiguity_slots:
            if slot["materiality"] != "material":
                continue
            decision = decision_ledger.active_for_slot(str(slot["slot_id"]))
            if decision is None or decision.status not in {
                "inferred",
                "user_confirmed",
            }:
                raise PlanAuthorityContractError(
                    f"plan_compiler_material_decision_unresolved:{slot['slot_id']}"
                )
        if supersedes_plan_revision is None:
            return
        if not isinstance(supersedes_plan_revision, PlanRevision):
            raise PlanAuthorityContractError("plan_compiler_supersedes_invalid")
        if (
            supersedes_plan_revision.run_attempt_id != intent_revision.run_attempt_id
            or supersedes_plan_revision.intent_revision_id
            != intent_revision.intent_revision_id
            or supersedes_plan_revision.authority_context_ref
            != authority_context.authority_context_ref
        ):
            raise PlanAuthorityContractError(
                "plan_compiler_supersedes_authority_context_mismatch"
            )
        if (
            supersedes_plan_revision.budget_policy_ref
            != self._registry.exploration_budget_policy.budget_policy_ref
        ):
            raise PlanAuthorityContractError(
                "plan_compiler_supersedes_budget_policy_mismatch"
            )

    def _compile_goal_plans(
        self, intent_revision: IntentRevision
    ) -> tuple[Mapping[str, Any], ...]:
        plans = []
        for target_metric in intent_revision.target_metric_refs:
            plans.append(
                self._registry.compile_goal_analysis_plan(
                    goal_bindings=intent_revision.goal_bindings,
                    target_metric=target_metric,
                    explicit_focus={
                        "component_ids": [],
                        "dimension_ids": [],
                        "context_source_ids": [],
                    },
                )
            )
        if not plans:
            raise PlanAuthorityContractError("plan_compiler_target_metric_missing")
        return tuple(plans)

    def _mandatory_obligations(
        self,
        *,
        intent_revision: IntentRevision,
        goal_plans: Sequence[Mapping[str, Any]],
        temporal_authority: EffectiveTemporalComparison,
    ) -> tuple[ClaimObligation, ...]:
        specs: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        for plan in goal_plans:
            target_metric = str(plan["target_metric"])
            for goal_binding in plan["goal_bindings"]:
                goal_id = str(goal_binding["goal_id"])
                goal = self._registry.analysis_goal_obligation(goal_id)
                for outcome_ref in goal["required_outcomes"]:
                    for claim_kind in goal["outcome_claim_types"][outcome_ref]:
                        key = (target_metric, str(claim_kind))
                        spec = specs.setdefault(
                            key,
                            {"outcome_refs": [], "goal_refs": []},
                        )
                        if outcome_ref not in spec["outcome_refs"]:
                            spec["outcome_refs"].append(str(outcome_ref))
                        if goal_id not in spec["goal_refs"]:
                            spec["goal_refs"].append(goal_id)
        obligations = []
        for (target_metric, claim_kind), spec in specs.items():
            evidence_types = self._evidence_types_for_claim(
                claim_kind=claim_kind,
                goal_plans=goal_plans,
                temporal_authority=temporal_authority,
            )
            success_policy: dict[str, Any] = {
                "policy": "verified_or_explicit_boundary",
                "minimum_claim_strength": (
                    self._registry.claim_required_publication_strength(
                        claim_kind,
                        goal_ids=tuple(spec["goal_refs"]),
                    )
                ),
                "outcome_refs": tuple(spec["outcome_refs"]),
            }
            composite_policy = self._registry.claim_composite_support_policy(claim_kind)
            if composite_policy is not None:
                success_policy["composite_support_policy"] = composite_policy
            obligations.append(
                ClaimObligation.create(
                    claim_kind=claim_kind,
                    role="user_required",
                    subject={
                        "target_metric_ref": target_metric,
                        "scope": intent_revision.scope,
                        "outcome_refs": tuple(spec["outcome_refs"]),
                        "goal_refs": tuple(spec["goal_refs"]),
                    },
                    evidence_requirement=EvidenceRequirement.create(
                        operator="any_of",
                        evidence_kinds=evidence_types,
                    ),
                    success_policy=success_policy,
                )
            )
        if not obligations:
            raise PlanAuthorityContractError("plan_compiler_obligations_missing")
        return tuple(obligations)

    def _evidence_types_for_claim(
        self,
        *,
        claim_kind: str,
        goal_plans: Sequence[Mapping[str, Any]],
        temporal_authority: EffectiveTemporalComparison,
    ) -> tuple[str, ...]:
        evidence_types: list[str] = []
        for plan in goal_plans:
            for axis in plan["analysis_axes"]:
                for capability_id in axis["capability_refs"]:
                    contract = self._registry.capability_inputs(str(capability_id))
                    if not capability_supports_temporal_authority(
                        contract,
                        temporal_authority,
                    ):
                        continue
                    if claim_kind not in contract.get("supported_claim_types", ()):
                        continue
                    for evidence_type in contract.get("supported_evidence_types", ()):
                        if evidence_type not in evidence_types:
                            evidence_types.append(str(evidence_type))
        if not evidence_types:
            raise PlanAuthorityContractError(
                "plan_compiler_claim_has_no_temporal_evidence_contract:"
                f"{claim_kind}:{temporal_authority.mode}"
            )
        return publication_evidence_kinds(tuple(evidence_types))

    def _requested_factor_obligations(
        self,
        *,
        intent_revision: IntentRevision,
        temporal_authority: EffectiveTemporalComparison,
    ) -> tuple[ClaimObligation, ...]:
        goal_ids = tuple(
            str(binding["goal_id"]) for binding in intent_revision.goal_bindings
        )
        obligations: list[ClaimObligation] = []
        for factor_index, factor_ref in enumerate(
            intent_revision.requested_factor_refs
        ):
            claim_kind, axis_ids, evidence_types = self._requested_factor_route(
                factor_ref=str(factor_ref),
                intent_revision=intent_revision,
                temporal_authority=temporal_authority,
            )
            success_policy: dict[str, Any] = {
                "policy": "verified_or_explicit_boundary",
                "minimum_claim_strength": (
                    self._registry.claim_required_publication_strength(
                        claim_kind,
                        goal_ids=goal_ids,
                        axis_ids=axis_ids,
                    )
                ),
                "outcome_refs": ("requested_factor_evidence",),
                "requested_axis_ids": axis_ids,
                "requested_dimension_refs": tuple(
                    dict.fromkeys(
                        dimension_ref
                        for axis_id in axis_ids
                        for dimension_ref in self._registry.analysis_axis(
                            axis_id
                        )["dimension_refs"]
                    )
                ),
                "dimension_summary_anchor": factor_index == 0,
            }
            composite_policy = self._registry.claim_composite_support_policy(
                claim_kind
            )
            if composite_policy is not None:
                success_policy["composite_support_policy"] = composite_policy
            obligations.append(
                ClaimObligation.create(
                    claim_kind=claim_kind,
                    role="user_required",
                    subject={
                        "target_metric_ref": str(factor_ref),
                        "scope": intent_revision.scope,
                        "outcome_refs": ("requested_factor_evidence",),
                        "goal_refs": goal_ids,
                    },
                    evidence_requirement=EvidenceRequirement.create(
                        operator="any_of",
                        evidence_kinds=evidence_types,
                    ),
                    success_policy=success_policy,
                )
            )
        return tuple(obligations)

    def _requested_factor_route(
        self,
        *,
        factor_ref: str,
        intent_revision: IntentRevision,
        temporal_authority: EffectiveTemporalComparison,
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        relevant_axis_ids = tuple(
            axis_id
            for axis_id in dict.fromkeys(
                (
                    *intent_revision.requested_analysis_axes,
                    *self._registry.analysis_axis_ids,
                )
            )
            if factor_ref
            in set(self._registry.analysis_axis(axis_id)["metric_refs"])
        )
        metric_claim_kinds = tuple(
            dict.fromkeys(
                str(claim_kind)
                for source in self._registry.metric_sources(factor_ref).values()
                for claim_kind in source.get("claim_types", ())
            )
        )
        for claim_kind in metric_claim_kinds:
            axis_ids, evidence_types = self._factor_claim_route(
                claim_kind=claim_kind,
                axis_ids=relevant_axis_ids,
                temporal_authority=temporal_authority,
            )
            if axis_ids:
                return claim_kind, axis_ids, evidence_types

        boundary_axis_ids = tuple(
            axis_id
            for axis_id in self._registry.analysis_axis_ids
            if set(
                self._registry.analysis_axis(axis_id)["target_metric_refs"]
            ).intersection(intent_revision.target_metric_refs)
        )
        axis_ids, evidence_types = self._factor_claim_route(
            claim_kind="contract_coverage_and_trust_boundary",
            axis_ids=boundary_axis_ids,
            temporal_authority=temporal_authority,
        )
        if not axis_ids:
            raise PlanAuthorityContractError(
                "plan_compiler_requested_factor_boundary_route_missing:"
                + factor_ref
            )
        return "contract_coverage_and_trust_boundary", axis_ids, evidence_types

    def _factor_claim_route(
        self,
        *,
        claim_kind: str,
        axis_ids: Sequence[str],
        temporal_authority: EffectiveTemporalComparison,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        routed_axis_ids: list[str] = []
        evidence_types: list[str] = []
        for axis_id in axis_ids:
            axis_has_route = False
            for capability_id in self._registry.analysis_axis(axis_id)[
                "capability_refs"
            ]:
                capability = self._registry.capability_inputs(
                    str(capability_id)
                )
                if capability.get("completion_authority"):
                    continue
                if not capability_supports_temporal_authority(
                    capability,
                    temporal_authority,
                ):
                    continue
                if claim_kind not in capability.get(
                    "supported_claim_types", ()
                ):
                    continue
                axis_has_route = True
                for evidence_type in capability.get(
                    "supported_evidence_types", ()
                ):
                    if evidence_type not in evidence_types:
                        evidence_types.append(str(evidence_type))
            if axis_has_route:
                routed_axis_ids.append(str(axis_id))
        if not routed_axis_ids:
            return (), ()
        return (
            tuple(routed_axis_ids),
            publication_evidence_kinds(tuple(evidence_types)),
        )

    def _merge_goal_axes(
        self, goal_plans: Sequence[Mapping[str, Any]]
    ) -> OrderedDict[str, dict[str, Any]]:
        merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for plan in goal_plans:
            target_metric = str(plan["target_metric"])
            for axis in plan["analysis_axes"]:
                axis_id = str(axis["axis_id"])
                if axis_id not in merged:
                    merged[axis_id] = {
                        "axis_id": axis_id,
                        "role": str(axis["role"]),
                        "target_metric_refs": [target_metric],
                        "goal_refs": list(axis["goal_refs"]),
                    }
                    continue
                record = merged[axis_id]
                if target_metric not in record["target_metric_refs"]:
                    record["target_metric_refs"].append(target_metric)
                for goal_ref in axis["goal_refs"]:
                    if goal_ref not in record["goal_refs"]:
                        record["goal_refs"].append(str(goal_ref))
                if (
                    _AXIS_ROLE_PRIORITY[str(axis["role"])]
                    > _AXIS_ROLE_PRIORITY[record["role"]]
                ):
                    record["role"] = str(axis["role"])
        return merged

    def _effective_temporal_authority(
        self,
        *,
        intent_revision: IntentRevision,
        decision_ledger: DecisionLedger,
        goal_axes: Mapping[str, Mapping[str, Any]],
    ) -> EffectiveTemporalComparison:
        requires_baseline = any(
            record["role"] == "required"
            and self._registry.analysis_axis(axis_id)["selection_policy"]
            == "primary_baseline_required"
            for axis_id, record in goal_axes.items()
        )
        try:
            return resolve_effective_comparison(
                time_spec=intent_revision.time_spec,
                comparison_spec=intent_revision.comparison_spec,
                decision_ledger=decision_ledger,
                require_physical_baseline=requires_baseline,
            )
        except TemporalComparisonContractError as exc:
            raise PlanAuthorityContractError(
                f"plan_compiler_temporal_authority_invalid:{exc}"
            ) from exc

    def _admit_proposal(
        self,
        *,
        intent_revision: IntentRevision,
        authority_context: AuthorityContext,
        planner_proposal: PlannerProposal,
        goal_axes: Mapping[str, Mapping[str, Any]],
        mandatory_obligations: Sequence[ClaimObligation],
        resolved_window_refs: Sequence[str],
    ) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
        del authority_context
        entries: list[Mapping[str, Any]] = []
        admitted_axis_items: dict[str, str] = {}
        admitted_axis_item_ids: set[str] = set()
        for item in planner_proposal.auxiliary_axes:
            item_id = str(item["proposal_item_id"])
            axis_id = str(item["axis_id"])
            if axis_id not in set(self._registry.analysis_axis_ids):
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="analysis_axis",
                        status="rejected",
                        reason_code="unknown_axis_ref",
                    )
                )
                continue
            axis = self._registry.analysis_axis(axis_id)
            if not set(intent_revision.target_metric_refs).issubset(
                set(axis["target_metric_refs"])
            ):
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="analysis_axis",
                        status="rejected",
                        reason_code="axis_target_metric_unsupported",
                        contract_refs=axis["source_refs"],
                    )
                )
                continue
            supported_claims = self._axis_supported_claims(axis_id)
            if not set(item["supports_claim_kinds"]).issubset(supported_claims):
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="analysis_axis",
                        status="rejected",
                        reason_code="axis_claim_kind_unsupported",
                        contract_refs=self._axis_contract_refs(axis_id),
                    )
                )
                continue
            if axis_id in admitted_axis_items:
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="analysis_axis",
                        status="rejected",
                        reason_code="duplicate_execution_ref",
                        contract_refs=self._axis_contract_refs(axis_id),
                    )
                )
                continue
            admitted_axis_items[axis_id] = item_id
            admitted_axis_item_ids.add(item_id)
            entries.append(
                _admission_entry(
                    item_id=item_id,
                    item_kind="analysis_axis",
                    status="admitted",
                    reason_code="supported_auxiliary_axis",
                    contract_refs=self._axis_contract_refs(axis_id),
                    normalized_execution_ref=axis_id,
                )
            )

        known_assumption_refs = {
            str(item["proposal_item_id"])
            for item in planner_proposal.assumption_proposals
        }
        known_refs = {
            *self._registry.analysis_axis_ids,
            *self._registry.metric_ids,
            *intent_revision.target_metric_refs,
            *resolved_window_refs,
            *planner_proposal.decision_refs,
        }
        admitted_assumption_items: set[str] = set()
        assumption_entries: list[Mapping[str, Any]] = []
        for item in planner_proposal.assumption_proposals:
            item_id = str(item["proposal_item_id"])
            unknown = set(item["affected_refs"]) - known_refs
            if unknown:
                assumption_entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="assumption",
                        status="rejected",
                        reason_code="assumption_affected_ref_unknown",
                    )
                )
                continue
            admitted_assumption_items.add(item_id)
            assumption_entries.append(
                _admission_entry(
                    item_id=item_id,
                    item_kind="assumption",
                    status="admitted",
                    reason_code="assumption_refs_contract_bound",
                    contract_refs=tuple(item["affected_refs"]),
                    normalized_execution_ref=f"assumption:{item_id}",
                )
            )

        admissible_axis_ids = {
            *goal_axes,
            *intent_revision.requested_analysis_axes,
            *admitted_axis_items,
        }
        admitted_hypothesis_items: set[str] = set()
        hypothesis_axis_refs: dict[str, tuple[str, ...]] = {}
        for item in planner_proposal.hypotheses:
            item_id = str(item["proposal_item_id"])
            requested_axes = tuple(str(ref) for ref in item["requested_axis_ids"])
            if not requested_axes:
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="hypothesis",
                        status="rejected",
                        reason_code="hypothesis_axis_ref_missing",
                    )
                )
                continue
            if any(
                axis_id not in set(self._registry.analysis_axis_ids)
                for axis_id in requested_axes
            ):
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="hypothesis",
                        status="rejected",
                        reason_code="requested_axis_not_admitted",
                    )
                )
                continue
            if not set(item["assumption_refs"]).issubset(
                admitted_assumption_items
            ) or not set(item["assumption_refs"]).issubset(known_assumption_refs):
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="hypothesis",
                        status="rejected",
                        reason_code="hypothesis_assumption_not_admitted",
                    )
                )
                continue
            claim_kind = str(item["target_claim_kind"])
            if not any(
                claim_kind in self._axis_supported_claims(axis_id)
                for axis_id in requested_axes
            ):
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="hypothesis",
                        status="rejected",
                        reason_code="hypothesis_claim_kind_unsupported",
                        contract_refs=tuple(
                            ref
                            for axis_id in requested_axes
                            for ref in self._axis_contract_refs(axis_id)
                        ),
                    )
                )
                continue
            admissible_axis_ids.update(requested_axes)
            admitted_hypothesis_items.add(item_id)
            hypothesis_axis_refs[item_id] = requested_axes
            entries.append(
                _admission_entry(
                    item_id=item_id,
                    item_kind="hypothesis",
                    status="admitted",
                    reason_code="hypothesis_contract_bound",
                    contract_refs=tuple(
                        ref
                        for axis_id in requested_axes
                        for ref in self._axis_contract_refs(axis_id)
                    ),
                    normalized_execution_ref=f"hypothesis:{item_id}",
                )
            )

        mandatory_claim_kinds = {
            obligation.claim_kind for obligation in mandatory_obligations
        }
        included_axis_ids = self._scheduled_axis_ids(
            intent_revision=intent_revision,
            goal_axes=goal_axes,
            mandatory_claim_kinds=mandatory_claim_kinds,
            admitted_axis_ids=set(admitted_axis_items),
            hypothesis_axis_ids={
                axis_id
                for axis_refs in hypothesis_axis_refs.values()
                for axis_id in axis_refs
            },
        )
        admitted_priority_items: dict[str, str] = {}
        for item in planner_proposal.priority_proposals:
            item_id = str(item["proposal_item_id"])
            target_ref = str(item["target_ref"])
            if target_ref not in admissible_axis_ids:
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="priority",
                        status="rejected",
                        reason_code="priority_target_ref_unknown",
                    )
                )
            elif target_ref not in included_axis_ids:
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="priority",
                        status="deferred",
                        reason_code="priority_target_not_scheduled",
                        contract_refs=self._axis_contract_refs(target_ref),
                    )
                )
            elif target_ref in admitted_priority_items:
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="priority",
                        status="rejected",
                        reason_code="duplicate_execution_ref",
                        contract_refs=self._axis_contract_refs(target_ref),
                    )
                )
            else:
                admitted_priority_items[target_ref] = item_id
                entries.append(
                    _admission_entry(
                        item_id=item_id,
                        item_kind="priority",
                        status="admitted",
                        reason_code="priority_target_scheduled",
                        contract_refs=self._axis_contract_refs(target_ref),
                        normalized_execution_ref=target_ref,
                    )
                )
        entries.extend(assumption_entries)
        return tuple(entries), {
            "axis_items": admitted_axis_items,
            "hypothesis_items": admitted_hypothesis_items,
            "hypothesis_axis_refs": hypothesis_axis_refs,
            "priority_items": admitted_priority_items,
            "assumption_items": admitted_assumption_items,
            "assumption_refs": tuple(
                f"assumption:{item_id}" for item_id in admitted_assumption_items
            ),
            "included_axis_ids": included_axis_ids,
        }

    def _scheduled_axis_ids(
        self,
        *,
        intent_revision: IntentRevision,
        goal_axes: Mapping[str, Mapping[str, Any]],
        mandatory_claim_kinds: set[str],
        admitted_axis_ids: set[str],
        hypothesis_axis_ids: set[str],
    ) -> set[str]:
        included = {
            axis_id
            for axis_id, record in goal_axes.items()
            if record["role"] in {"required", "disclosure"}
        }
        covered_claims = {
            claim_kind
            for axis_id in included
            for claim_kind in self._axis_supported_claims(axis_id)
        }
        for axis_id in goal_axes:
            claims = self._axis_supported_claims(axis_id) & mandatory_claim_kinds
            if claims - covered_claims:
                included.add(axis_id)
                covered_claims.update(claims)
        included.update(intent_revision.requested_analysis_axes)
        included.update(admitted_axis_ids)
        included.update(hypothesis_axis_ids)
        active_goal_ids = tuple(
            dict.fromkeys(
                str(goal_ref)
                for record in goal_axes.values()
                for goal_ref in record["goal_refs"]
            )
        )
        for factor_domain_id in self._registry.factor_domain_ids_for_goals(
            active_goal_ids,
            target_metric=str(intent_revision.target_metric_refs[0]),
        ):
            included.update(
                self._registry.factor_domain(factor_domain_id)["axis_refs"]
            )
        return included

    def _analyst_obligations(
        self,
        *,
        intent_revision: IntentRevision,
        planner_proposal: PlannerProposal,
        admitted: Mapping[str, Any],
    ) -> tuple[ClaimObligation, ...]:
        obligations = []
        admitted_ids = set(admitted["hypothesis_items"])
        active_goal_ids = tuple(
            str(binding["goal_id"]) for binding in intent_revision.goal_bindings
        )
        for hypothesis in planner_proposal.hypotheses:
            item_id = str(hypothesis["proposal_item_id"])
            if item_id not in admitted_ids:
                continue
            axis_ids = tuple(admitted["hypothesis_axis_refs"][item_id])
            evidence_types: list[str] = []
            claim_kind = str(hypothesis["target_claim_kind"])
            for axis_id in axis_ids:
                axis = self._registry.analysis_axis(axis_id)
                for capability_id in axis["capability_refs"]:
                    capability = self._registry.capability_inputs(capability_id)
                    if claim_kind not in capability.get("supported_claim_types", ()):
                        continue
                    for evidence_type in capability.get("supported_evidence_types", ()):
                        if evidence_type not in evidence_types:
                            evidence_types.append(str(evidence_type))
            if not evidence_types:
                raise PlanAuthorityContractError(
                    f"plan_compiler_hypothesis_evidence_contract_missing:{item_id}"
                )
            success_policy: dict[str, Any] = {
                "policy": "verified_or_explicit_boundary",
                "minimum_claim_strength": (
                    self._registry.claim_required_publication_strength(
                        claim_kind,
                        goal_ids=active_goal_ids,
                        axis_ids=axis_ids,
                    )
                ),
                "requested_axis_ids": axis_ids,
            }
            composite_policy = self._registry.claim_composite_support_policy(claim_kind)
            if composite_policy is not None:
                success_policy["composite_support_policy"] = composite_policy
            obligations.append(
                ClaimObligation.create(
                    claim_kind=claim_kind,
                    role="analyst_auxiliary",
                    subject={
                        "planner_proposal_ref": planner_proposal.planner_proposal_id,
                        "proposal_item_ref": item_id,
                        "target_metric_refs": intent_revision.target_metric_refs,
                        "scope": intent_revision.scope,
                        "goal_refs": active_goal_ids,
                    },
                    evidence_requirement=EvidenceRequirement.create(
                        operator="any_of",
                        evidence_kinds=publication_evidence_kinds(
                            tuple(evidence_types)
                        ),
                    ),
                    success_policy=success_policy,
                )
            )
        return tuple(obligations)

    def _preserve_superseded_obligations(
        self,
        *,
        source_plan: PlanRevision,
        candidate_obligations: Sequence[ClaimObligation],
    ) -> tuple[ClaimObligation, ...]:
        candidate_by_id = {
            obligation.obligation_id: obligation for obligation in candidate_obligations
        }
        if len(candidate_by_id) != len(candidate_obligations):
            raise PlanAuthorityContractError(
                "plan_compiler_candidate_obligation_duplicated"
            )
        candidate_by_proposal_key: dict[tuple[str, str], ClaimObligation] = {}
        for obligation in candidate_obligations:
            proposal_key = _proposal_obligation_key(obligation)
            if proposal_key is None:
                continue
            if proposal_key in candidate_by_proposal_key:
                raise PlanAuthorityContractError(
                    "plan_compiler_candidate_proposal_obligation_duplicated"
                )
            candidate_by_proposal_key[proposal_key] = obligation

        preserved: list[ClaimObligation] = []
        consumed_candidate_ids: set[str] = set()
        for source in source_plan.claim_obligations:
            candidate = candidate_by_id.get(source.obligation_id)
            if candidate is None:
                proposal_key = _proposal_obligation_key(source)
                if proposal_key is not None:
                    candidate = candidate_by_proposal_key.get(proposal_key)
            if candidate is None:
                raise PlanAuthorityContractError(
                    "plan_compiler_superseded_obligation_missing:"
                    + source.obligation_id
                )
            if _stable_obligation_projection(candidate) != (
                _stable_obligation_projection(source)
            ):
                raise PlanAuthorityContractError(
                    "plan_compiler_superseded_obligation_mutated:"
                    + source.obligation_id
                )
            preserved.append(source)
            consumed_candidate_ids.add(candidate.obligation_id)

        preserved.extend(
            obligation
            for obligation in candidate_obligations
            if obligation.obligation_id not in consumed_candidate_ids
        )
        return tuple(preserved)

    def _analysis_axes(
        self,
        *,
        intent_revision: IntentRevision,
        planner_proposal: PlannerProposal,
        goal_axes: Mapping[str, Mapping[str, Any]],
        claim_obligations: Sequence[ClaimObligation],
        admitted: Mapping[str, Any],
    ) -> tuple[AnalysisAxis, ...]:
        included_axis_ids = set(admitted["included_axis_ids"])
        priority_index = {
            axis_id: index for index, axis_id in enumerate(admitted["priority_items"])
        }
        natural_order = {
            axis_id: index
            for index, axis_id in enumerate(self._registry.analysis_axis_ids)
        }
        ordered_axis_ids = sorted(
            included_axis_ids,
            key=lambda axis_id: (
                priority_index.get(axis_id, len(priority_index)),
                natural_order[axis_id],
            ),
        )
        hypothesis_by_axis: dict[str, list[str]] = {}
        for item_id, axis_refs in admitted["hypothesis_axis_refs"].items():
            for axis_id in axis_refs:
                hypothesis_by_axis.setdefault(axis_id, []).append(item_id)
        obligations_by_kind: dict[str, list[ClaimObligation]] = {}
        for obligation in claim_obligations:
            obligations_by_kind.setdefault(obligation.claim_kind, []).append(obligation)
        proposal_axis_items = admitted["axis_items"]
        axes = []
        for axis_id in ordered_axis_ids:
            contract = self._registry.analysis_axis(axis_id)
            goal_record = goal_axes.get(axis_id)
            role = str(goal_record["role"]) if goal_record else "auxiliary"
            if axis_id in set(intent_revision.requested_analysis_axes) and role in {
                "auxiliary",
                "conditional",
            }:
                role = "required"
            axis_supported_claims = self._axis_supported_claims(axis_id)
            axis_target_metric_refs = tuple(
                metric_ref
                for metric_ref in intent_revision.target_metric_refs
                if metric_ref in set(contract["target_metric_refs"])
            )
            supported_obligation_ids = tuple(
                obligation.obligation_id
                for claim_kind in axis_supported_claims
                for obligation in obligations_by_kind.get(claim_kind, ())
                if (
                    (
                        obligation.role == "user_required"
                        and (
                            str(obligation.subject["target_metric_ref"])
                            in set(axis_target_metric_refs)
                            or (
                                str(obligation.subject["target_metric_ref"])
                                in set(intent_revision.requested_factor_refs)
                                and (
                                    str(
                                        obligation.subject[
                                            "target_metric_ref"
                                        ]
                                    )
                                    in set(contract["metric_refs"])
                                    or axis_id
                                    in set(
                                        obligation.success_policy.get(
                                            "requested_axis_ids", ()
                                        )
                                    )
                                )
                            )
                        )
                    )
                    or (
                        obligation.role != "user_required"
                        and obligation.subject.get("proposal_item_ref")
                        in set(hypothesis_by_axis.get(axis_id, ()))
                    )
                )
            )
            proposal_refs = []
            if axis_id in proposal_axis_items:
                proposal_refs.append(str(proposal_axis_items[axis_id]))
            proposal_refs.extend(hypothesis_by_axis.get(axis_id, ()))
            axes.append(
                AnalysisAxis.create(
                    axis_id=axis_id,
                    role=role,
                    axis_kind=contract["axis_kind"],
                    target_metric_refs=axis_target_metric_refs,
                    metric_refs=tuple(contract["metric_refs"]),
                    dimension_refs=tuple(contract["dimension_refs"]),
                    context_source_refs=tuple(contract["context_source_refs"]),
                    capability_refs=tuple(contract["capability_refs"]),
                    reconciliation_group=contract["reconciliation_group"],
                    selection_policy=contract["selection_policy"],
                    source_refs=tuple(contract["source_refs"]),
                    goal_refs=(
                        tuple(goal_record["goal_refs"])
                        if goal_record
                        else tuple(
                            str(binding["goal_id"])
                            for binding in intent_revision.goal_bindings
                        )
                    ),
                    supports_obligation_ids=tuple(
                        dict.fromkeys(supported_obligation_ids)
                    ),
                    proposal_refs=tuple(proposal_refs),
                )
            )
        return tuple(axes)

    def _capability_task_specs(
        self,
        *,
        authority_context: AuthorityContext,
        resolved_window_refs: Sequence[str],
        claim_obligations: Sequence[ClaimObligation],
        analysis_axes: Sequence[AnalysisAxis],
        temporal_authority: EffectiveTemporalComparison,
    ) -> tuple[Mapping[str, Any], ...]:
        coverage = {
            str(item["dataset_id"]): item for item in authority_context.dataset_coverage
        }
        obligations = {
            obligation.obligation_id: obligation for obligation in claim_obligations
        }
        tasks: list[dict[str, Any]] = []
        for axis in analysis_axes:
            axis_obligations = {
                obligation_id: obligations[obligation_id]
                for obligation_id in axis.supports_obligation_ids
            }
            for capability_id in axis.capability_refs:
                contract = self._registry.capability_inputs(capability_id)
                if contract.get("completion_authority"):
                    # A checkpoint/verifier-owned node consumes settled task
                    # authority in a later phase. It is not executable work in
                    # the Phase 3 capability DAG.
                    continue
                if not capability_supports_temporal_authority(
                    contract,
                    temporal_authority,
                ):
                    continue
                dataset_ids = self._capability_dataset_ids(
                    contract=contract,
                    axis=axis,
                )
                missing_coverage = set(dataset_ids) - set(coverage)
                if missing_coverage:
                    raise PlanAuthorityContractError(
                        "plan_compiler_authority_coverage_missing:"
                        + ",".join(sorted(missing_coverage))
                    )
                supported_claims = set(contract.get("supported_claim_types", ()))
                supported = tuple(
                    obligation
                    for obligation in axis_obligations.values()
                    if obligation.claim_kind in supported_claims
                )
                obligation_edges = tuple(
                    {
                        "obligation_id": obligation.obligation_id,
                        "required": obligation.role == "user_required",
                    }
                    for obligation in supported
                )
                has_required_obligation = any(
                    bool(edge["required"]) for edge in obligation_edges
                )
                has_obligation = bool(obligation_edges)
                supported_evidence_types = set(
                    contract.get("supported_evidence_types") or ()
                )
                degradation = dict(contract.get("degradation_policy") or {})
                if "missing_required_input" not in degradation:
                    raise PlanAuthorityContractError(
                        f"plan_compiler_degradation_contract_missing:{capability_id}"
                    )
                input_states = tuple(
                    {
                        "input_ref": f"dataset:{dataset_id}",
                        "availability": coverage[dataset_id]["availability"],
                        "limitation_ref": coverage[dataset_id]["limitation_ref"],
                    }
                    for dataset_id in dataset_ids
                )
                input_refs = tuple(
                    dict.fromkeys(
                        (
                            authority_context.authority_context_ref,
                            axis.analysis_axis_ref,
                            temporal_authority.authority_ref,
                            *resolved_window_refs,
                            *(f"metric:{item}" for item in axis.target_metric_refs),
                            *(f"metric:{item}" for item in axis.metric_refs),
                            *(f"dimension:{item}" for item in axis.dimension_refs),
                            *(
                                f"context-source:{item}"
                                for item in axis.context_source_refs
                            ),
                            *(f"dataset:{item}" for item in dataset_ids),
                            *(
                                f"factor-domain:{item}"
                                for item in self._registry.factor_domain_ids_for_axis(
                                    axis.axis_id
                                )
                            ),
                            self._registry.capability_contract_ref(capability_id),
                        )
                    )
                )
                task = {
                    "task_key": f"{axis.axis_id}:{capability_id}",
                    "capability_id": capability_id,
                    "normalized_input_refs": input_refs,
                    "dependency_task_keys": (),
                    "obligation_edges": obligation_edges,
                    "execution_rank": len(tasks) + 1,
                    "declared_budget_units": (CAPABILITY_TASK_DECLARED_BUDGET_UNITS),
                    "governor_inputs": {
                        "expected_information_gain": (
                            "obligation_closing"
                            if has_required_obligation
                            else "hypothesis_testing"
                            if has_obligation
                            else "context_enrichment"
                        ),
                        "materiality": (
                            "user_required"
                            if has_required_obligation
                            else "analyst_auxiliary"
                            if has_obligation
                            else "contextual"
                        ),
                        "actionability": (
                            "decision_supporting"
                            if has_required_obligation
                            else "explanation_supporting"
                            if has_obligation
                            else "diagnostic"
                        ),
                        "statistical_risk": (
                            "multiplicity_sensitive"
                            if "statistical_association" in supported_evidence_types
                            else "contract_bounded"
                        ),
                    },
                    "execution_policy": {
                        "degradation_policy": degradation,
                        "integrity_failure": "fail_closed",
                        "input_states": input_states,
                    },
                }
                tasks.append(task)
        required_obligation_ids = {
            obligation.obligation_id
            for obligation in claim_obligations
            if obligation.role == "user_required"
        }
        covered_required_ids = {
            str(edge["obligation_id"])
            for task in tasks
            for edge in task["obligation_edges"]
            if edge["required"]
        }
        uncovered = required_obligation_ids - covered_required_ids
        if uncovered:
            raise PlanAuthorityContractError(
                "plan_compiler_temporal_obligation_path_missing:"
                + ",".join(sorted(uncovered))
            )
        return tuple(tasks)

    def _capability_dataset_ids(
        self,
        *,
        contract: Mapping[str, Any],
        axis: AnalysisAxis,
    ) -> tuple[str, ...]:
        if contract.get("source_mode") == "requested_context_sources":
            allowed = set(contract.get("allowed_context_datasets", ()))
            return tuple(
                dataset_id
                for dataset_id in axis.context_source_refs
                if dataset_id in allowed
            )
        return tuple(str(item) for item in contract.get("allowed_datasets", ()))

    def _bind_capability_task_dependencies(
        self,
        task_specs: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        by_key = {str(item["task_key"]): dict(item) for item in task_specs}
        if len(by_key) != len(task_specs):
            raise PlanAuthorityContractError("plan_compiler_task_key_duplicated")
        for task_key, task in by_key.items():
            axis_id, separator, _ = task_key.rpartition(":")
            if not separator or not axis_id:
                raise PlanAuthorityContractError("plan_compiler_task_key_invalid")
            contract = self._registry.capability_inputs(str(task["capability_id"]))
            dependency_capabilities = tuple(
                str(item) for item in contract.get("task_dependencies") or ()
            )
            dependency_keys = tuple(
                f"{axis_id}:{capability_id}"
                for capability_id in dependency_capabilities
            )
            missing = tuple(item for item in dependency_keys if item not in by_key)
            if missing:
                raise PlanAuthorityContractError(
                    "plan_compiler_task_dependency_missing:"
                    f"{task['capability_id']}:" + ",".join(dependency_capabilities)
                )
            task["dependency_task_keys"] = dependency_keys

        ordered: list[dict[str, Any]] = []
        remaining = list(by_key)
        emitted: set[str] = set()
        while remaining:
            ready = tuple(
                key
                for key in remaining
                if set(by_key[key]["dependency_task_keys"]) <= emitted
            )
            if not ready:
                raise PlanAuthorityContractError("plan_compiler_task_dependency_cycle")
            for key in ready:
                remaining.remove(key)
                emitted.add(key)
                ordered.append(by_key[key])
        return tuple(
            {**item, "execution_rank": index}
            for index, item in enumerate(ordered, start=1)
        )

    def _compile_context_window_specs(
        self,
        task_specs: Sequence[Mapping[str, Any]],
        *,
        temporal_authority: EffectiveTemporalComparison,
    ) -> tuple[PlanContextWindowSpec, ...]:
        if temporal_authority.mode == "calendar_partition":
            return ()
        capability_ids = tuple(
            dict.fromkeys(str(task["capability_id"]) for task in task_specs)
        )
        specs = []
        for capability_id in capability_ids:
            policy = self._registry.capability_inputs(capability_id).get(
                "context_window_policy"
            )
            if not isinstance(policy, Mapping):
                continue
            execution_default = policy["execution_default"]
            binding = self._registry.capability_inputs(capability_id).get(
                "task_input_binding"
            )
            if (
                isinstance(binding, Mapping)
                and binding.get("pattern_mode") == "rolling"
            ):
                bounds = policy["count_bounds"]["day"]
                strategy = resolve_rolling_window_strategy(
                    temporal_authority,
                    parameters=binding["parameters"],
                    maximum_context_days=bounds[1],
                )
                execution_default = {
                    "unit": "day",
                    "count": strategy.context_days,
                }
            specs.append(
                PlanContextWindowSpec.create(
                    capability_id=capability_id,
                    relation=str(policy["relation"]),
                    unit=str(execution_default["unit"]),
                    count=execution_default["count"],
                )
            )
        return tuple(specs)

    @staticmethod
    def _bind_context_window_specs(
        task_specs: Sequence[Mapping[str, Any]],
        context_window_specs: Sequence[PlanContextWindowSpec],
    ) -> tuple[Mapping[str, Any], ...]:
        spec_by_capability = {spec.capability_id: spec for spec in context_window_specs}
        return tuple(
            {
                **dict(task),
                "normalized_input_refs": tuple(
                    (
                        *task["normalized_input_refs"],
                        spec_by_capability[
                            str(task["capability_id"])
                        ].normalized_input_ref,
                    )
                    if str(task["capability_id"]) in spec_by_capability
                    else task["normalized_input_refs"]
                ),
            }
            for task in task_specs
        )

    @staticmethod
    def _preserve_superseded_task_ranks(
        *,
        source_plan: PlanRevision,
        candidate_task_specs: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        source_rank_by_key = {
            task.task_key: task.execution_rank for task in source_plan.capability_tasks
        }
        candidate_keys = {str(task["task_key"]) for task in candidate_task_specs}
        missing = set(source_rank_by_key) - candidate_keys
        if missing:
            raise PlanAuthorityContractError(
                "plan_compiler_superseded_task_missing:" + ",".join(sorted(missing))
            )
        next_rank = max(source_rank_by_key.values(), default=0) + 1
        ranked: list[Mapping[str, Any]] = []
        for task in candidate_task_specs:
            task_key = str(task["task_key"])
            rank = source_rank_by_key.get(task_key)
            if rank is None:
                rank = next_rank
                next_rank += 1
            ranked.append({**dict(task), "execution_rank": rank})
        return tuple(ranked)

    def _axis_supported_claims(self, axis_id: str) -> set[str]:
        axis = self._registry.analysis_axis(axis_id)
        return {
            str(claim_kind)
            for capability_id in axis["capability_refs"]
            for claim_kind in self._registry.capability_inputs(capability_id).get(
                "supported_claim_types", ()
            )
        }

    def _axis_contract_refs(self, axis_id: str) -> tuple[str, ...]:
        axis = self._registry.analysis_axis(axis_id)
        return tuple(
            dict.fromkeys(
                (
                    *axis["source_refs"],
                    *(
                        self._registry.capability_contract_ref(capability_id)
                        for capability_id in axis["capability_refs"]
                    ),
                )
            )
        )


def _proposal_obligation_key(
    obligation: ClaimObligation,
) -> tuple[str, str] | None:
    subject = obligation.subject
    planner_proposal_ref = subject.get("planner_proposal_ref")
    proposal_item_ref = subject.get("proposal_item_ref")
    if planner_proposal_ref is None and proposal_item_ref is None:
        return None
    if (
        not isinstance(planner_proposal_ref, str)
        or not planner_proposal_ref
        or not isinstance(proposal_item_ref, str)
        or not proposal_item_ref
    ):
        raise PlanAuthorityContractError(
            "plan_compiler_proposal_obligation_identity_invalid"
        )
    return obligation.role, proposal_item_ref


def _stable_obligation_projection(
    obligation: ClaimObligation,
) -> Mapping[str, Any]:
    payload = obligation.to_dict()
    payload.pop("obligation_id")
    payload.pop("content_digest")
    subject = dict(payload["subject"])
    subject.pop("planner_proposal_ref", None)
    payload["subject"] = subject
    return payload


def _admission_entry(
    *,
    item_id: str,
    item_kind: str,
    status: str,
    reason_code: str,
    contract_refs: Sequence[str] = (),
    normalized_execution_ref: str | None = None,
) -> Mapping[str, Any]:
    return {
        "proposal_item_ref": item_id,
        "item_kind": item_kind,
        "status": status,
        "reason_code": reason_code,
        "contract_refs": tuple(dict.fromkeys(str(item) for item in contract_refs)),
        "normalized_execution_ref": normalized_execution_ref,
    }
