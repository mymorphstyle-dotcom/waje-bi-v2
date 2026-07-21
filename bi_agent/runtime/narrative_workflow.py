from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    RecommendationRecord,
)
from bi_agent.runtime.claim_settlement import (
    ClaimSettlement,
    validate_typed_claim_settlement,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMOutputError,
    LLMProviderError,
    LLMTimeoutError,
)
from bi_agent.runtime.llm_client import parse_llm_structured_response_content
from bi_agent.runtime.narrative_authority import (
    NARRATIVE_BLOCK_ROLES,
    BlockLocalValidationReport,
    BlockVerificationAttempt,
    BlockVerifierReport,
    BlockVeto,
    NarrativeBlock,
    NarrativeDocument,
    NarrativeFactBinding,
    NarrativeWriterAttempt,
    PublicationFieldVisibilityPolicy,
    PublicClaimPalette,
    PublicFactDescriptor,
    PublicLimitation,
    RestrictedProviderResponse,
    SensitiveOutputFinding,
    narrative_block_authority_handles_are_valid,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
)


class NarrativeWorkflowError(ValueError):
    pass


class NarrativeProviderCallError(RuntimeError):
    """A typed failure confined to the external narrative provider call."""

    def __init__(
        self,
        *,
        kind: str,
        retryability: str,
        call_input_ref: str,
        technical_detail_ref: str,
    ) -> None:
        if kind not in {
            "provider_unavailable",
            "provider_timeout",
            "provider_configuration_invalid",
            "provider_output_invalid",
            "provider_authentication_failed",
            "provider_permission_denied",
            "provider_rate_limited",
            "provider_request_rejected",
            "narrative_input_budget_exceeded",
        }:
            raise NarrativeWorkflowError("narrative_provider_failure_kind_invalid")
        if retryability not in {"retryable", "not_retryable"}:
            raise NarrativeWorkflowError(
                "narrative_provider_failure_retryability_invalid"
            )
        if not isinstance(call_input_ref, str) or not call_input_ref.startswith(
            "narrative-provider-input:sha256:"
        ):
            raise NarrativeWorkflowError("narrative_provider_failure_input_ref_invalid")
        if (
            not isinstance(technical_detail_ref, str)
            or not technical_detail_ref.startswith("technical-detail:sha256:")
            or len(technical_detail_ref.removeprefix("technical-detail:sha256:")) != 64
        ):
            raise NarrativeWorkflowError(
                "narrative_provider_failure_detail_ref_invalid"
            )
        super().__init__(kind)
        self.kind = kind
        self.retryability = retryability
        self.call_input_ref = call_input_ref
        self.technical_detail_ref = technical_detail_ref


_PROVIDER_PURPOSES = frozenset({"narrative_writer", "block_verification"})
NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT = 512 * 1024
_NARRATIVE_PROMPT_VERSION = "single-authority-phase05.v13"
_WRITER_CONTRACT_FINDINGS_AUDIT_FIELD = "writer_contract_findings"
_WRITER_BLOCK_FIELDS = frozenset(
    {
        "role",
        "text",
        "claim_handles",
        "recommendation_handles",
        "limitation_handles",
        "material_fact_bindings",
        "statement_role",
        "required",
    }
)
_FOCUSED_WRITER_EDITABLE_BLOCK_FIELDS = frozenset(
    {
        "text",
        "claim_handles",
        "recommendation_handles",
        "limitation_handles",
        "material_fact_bindings",
        "statement_role",
    }
)
_FACT_BINDING_FIELDS = frozenset(
    {
        "claim_handle",
        "fact_handle",
    }
)
_VERIFIER_DECISION_FIELDS = frozenset(
    {
        "block_id",
        "disposition",
        "reason_code",
        "affected_claim_handles",
        "affected_recommendation_handles",
        "limitation_handles",
    }
)
_WRITER_SYSTEM_PROMPT = """\
Write an original, business-readable analysis using the supplied public material
projection and answer context. Claims declare material_handles; evidence_materials
hold the facts and evidence metadata authorized by those handles. A material fact may
be bound to a claim only when that claim includes the fact's material_handle.
Limitations reference pooled boundary_facets. Use the relevant facets to express the
exact boundary without repeating unrelated context. You may synthesize, compare,
qualify, prioritize, and develop decision-useful insight freely within each claim's
publication ceiling and exact material bindings. Evidence interpretation_contracts
govern which materials may be ranked, compared, or added. Overlapping slices whose
contract says additive=false may still support localization and diagnostic priority,
but they cannot become an additive contribution table or a shared-value ranking unless
the declared ranking basis authorizes that comparison. Synthesis contracts identify
fact groups that must stay auditable as a unit; bind every fact required by their
coverage_policy while choosing your own explanation and emphasis.
When one block compares two or more materials under a shared ranking_scope and uses
the declared ranking_measure, bind both ranking_measure and ranking_position_measure
for every compared item. Present those items in the exact order declared by
ranking_order and priority_rank_order. A score and its typed position are one ranking
fact pair; do not infer, renumber, or reorder either value.
When an interpretation contract declares a count partition, preserve the declared
whole-to-parts relationship exactly. A zero-filled group authorized by complete query
coverage and passed reconciliation is comparable; describe it as a reconciled zero,
not missing data. Never place an incomplete part inside the comparable part.

Recommendations expose verified typed commitments. Keep every action within its
declared domain and stage, and keep every expected outcome within its declared value
kind and mode. An investigate, validate, or experiment commitment cannot be strengthened
into an intervention, rollout, scale decision, promised recovery, or expected business
effect. A limitation or count applies only to its declared assertion_scope and
scope_effect. A local claim-family boundary cannot lower unrelated claims or the whole
analysis. coverage_semantics=supported_with_limitations means supported findings with a
bounded unavailable or weaker path; it does not assert conflicting evidence. Only an
explicit contradicted coverage semantic authorizes conflict or inconsistency language.
When a recommendation names multiple business subjects, preserve the evidence direction
and purpose for each subject separately. Do not apply weakness, underperformance,
remediation, replication, or scaling language to a subject unless that subject's own
typed diagnostic premises support it. Separate a positive growth-replication path from
a negative-deviation investigation instead of collapsing both into one ambiguous action.

Write every customer-facing block in answer_context.locale. Translate ordinary source
prose, management terms, and recommendation text into that locale. Preserve supplied
proper nouns, customer-safe business labels, and conventional abbreviations when useful,
but do not leave general source-language filler untranslated or produce avoidable mixed-
language prose.

Bind every claim, recommendation, limitation, and material fact to the supplied handle.
Material fact names, field identifiers, handles, enums, and snake_case tokens are
machine vocabulary. Translate them into natural business language and never copy them
into customer prose. Use supplied business labels where available; omit an internal
label when no customer-safe label is supplied.
Every explicit number, date, amount, range, ratio, score, or rank in a block must have
its own material_fact_binding on that block. Finding the same value elsewhere in the
projection does not authorize unbound prose.
Treat bound numeric values as exact unless their fact contract declares a range or
approximation. A ratio of 0.5 is exactly 50%; do not describe it as above, below, near,
or slightly different from 50%. Every qualitative numeric modifier must be logically
entailed by the bound value, threshold, range, or interpretation contract.
When typed limitation semantics declare source_availability=available and
evidence_role=background_context, say that the data was available and used for context
or candidate localization. Express the declared restriction on direct attribution or
causal conclusions. Do not call that source missing, unavailable, incomplete, or a data
contract gap unless an explicit typed completeness fact says so.
Never invent a handle, date, scope, label, recommendation, or stronger claim. You may
derive exact arithmetic relationships from bound numeric facts when the operands share
an authorized synthesis or comparison basis. Keep the exact prose you want published
in each text field. Return one JSON object with exactly this
shape: {"blocks":[{"role":string,"text":string,"claim_handles":[string],
"recommendation_handles":[string],"limitation_handles":[string],
"material_fact_bindings":[{"claim_handle":string,"fact_handle":string}],
"statement_role":string,"required":boolean}]}. Material fact bindings contain handles
only; the runtime resolves the fact kind, value, range end, and unit from the supplied
projection. Do not add fields outside this schema. Choose the block count, ordering,
roles, emphasis, and synthesis that best answer the question. Valid roles are
executive_answer, direction, accounting_drivers, dimension_localization,
contextual_pattern, boundary, and next_action. Mark the blocks whose meaning is
essential to the answer as required. Every non-boundary block must bind at least one
supplied claim or verified recommendation; next_action must bind a verified
recommendation. A boundary block must bind a limitation and may summarize limitations
from across the supplied projection. Other blocks may bind only the limitations attached
to their claims or the risks of recommendations they bind. In claim_bearing mode, at
least one required block must bind a supplied claim. In boundary_only mode, return one
required boundary block bound to supplied limitations and no claims, recommendations,
or material facts.

publication_requirements are mandatory user-requested answer obligations. Cover every
requirement through blocks marked required. A requirement counts as covered only through
its declared handles; required blocks may also bind other authorized handles for
integrated insight. For satisfied, mixed, and contradicted requirements, required blocks
must bind at least one listed claim_handle. For unavailable requirements, no claim is
required. For every status, required blocks must bind every listed limitation_handle.
Handles in optional blocks do not count. For satisfied, claim_handles have been filtered
to claims meeting required_claim_strength. Mixed and contradicted retain their coverage
claims, with limitation_handles carrying the strength gap or other boundary. These
handle-coverage rules do not prescribe the prose, block count, ordering, roles,
emphasis, comparison, or synthesis.

requirement_limitation_scope gives the same obligations with each required limitation's
boundary facets embedded directly. Treat this mapping as authoritative even when a
requirement-level limitation does not appear in a claim's local limitation_handles. Each
required limitation declares its typed binding_mode and claim_binding_options. In
claim_or_boundary mode, bind it to one of those claims or express it in a boundary block.
In boundary_only mode, express it in a boundary block and do not weaken accepted claims:
the limitation qualifies the unavailable analysis path described by its own typed
identity and outcome provenance. Use that provenance to state the concrete unavailable
business analysis path and its local effect. boundary_code is semantic input; translate
its meaning into business-readable prose instead of mechanically repeating the code.
Never repeat capability identifiers, retryability, provider fields, or other internal
metadata verbatim. Each bound limitation must be expressed through its own facets. Block
coverage is empty on the initial writer call because the runtime has not received your
blocks yet.
"""
_FOCUSED_WRITER_SYSTEM_PROMPT = """\
Repair the compiler-declared narrative targets using the supplied scoped public material
view and answer context. The runtime owns every target's identity, role, required value,
position, and replace-or-insert operation. Return one JSON object containing exactly one
editable block for every retry target, in retry_targets order. Return only editable target
fields with exactly this shape: {"blocks":[{"text":string,"claim_handles":[string],
"recommendation_handles":[string],"limitation_handles":[string],
"material_fact_bindings":[{"claim_handle":string,"fact_handle":string}],
"statement_role":string}]}. Never return target identity, role, required, source blocks,
or accepted siblings. The runtime restores its fixed fields and preserves untargeted
accepted siblings byte-for-byte.

The scoped material view is derived from the retry targets and open publication coverage.
Use allowed_claim_fact_pairs for every material fact binding. Use limitation_scope to keep
limitations on an authorized claim, recommendation risk, or a boundary target. Each retry
target declares its allowed_claim_handles, allowed_recommendation_handles, and
allowed_limitation_handles; use only those handles in that target. Satisfy every entry in
the target's required_coverage independently: bind at least one claim_handle_option when
the entry supplies options, and bind every required_limitation_handle. The
editable_source_block is only a repair seed. Its handle lists have already been stripped
of locally invalid relationships, while its prose may still require rewriting to match the
final handles. required_coverage is the remaining global publication obligation after
preserved required siblings are merged and must also hold across all repaired targets.
requirement_limitation_scope supplies the corresponding requirement-level claim options,
coverage semantics, assertion scope, typed binding topology, and embedded limitation
facets. Enforce each required limitation's binding_mode. In claim_or_boundary mode, bind the limitation to one of its
claim_binding_options or a boundary target. In boundary_only mode, keep it on a boundary
target and do not weaken accepted claims. Use its typed identity and outcome provenance
to express the concrete unavailable business analysis path and local effect. Treat
boundary_code as semantic input without mechanically repeating it, and never repeat
capability identifiers, retryability, provider fields, or other internal metadata
verbatim. Its source block coverage is context for the repair; satisfy the runtime-owned
retry target coverage in the returned blocks.

You may synthesize, compare, qualify, prioritize, and develop decision-useful insight
freely inside the supplied claim ceilings and exact bindings. Resolve fact details from
evidence_materials and never invent or rewrite a handle, number, date, scope, label,
recommendation, limitation, or stronger claim. Keep the exact prose intended for
publication in each text field. Preserve interpretation_contract, synthesis_contract,
recommendation commitment, coverage_semantics, assertion_scope, and limitation
scope_effect semantics exactly. supported_with_limitations does not imply conflicting
evidence, and a local claim-family limitation cannot become a global evidence boundary.
Write every repaired customer-facing block in answer_context.locale. Translate ordinary
source prose and management terms while preserving useful proper nouns, customer-safe
labels, and conventional abbreviations. For every named recommendation subject, preserve
its own evidence direction and purpose; keep positive replication and negative-deviation
investigation distinct when both occur in one recommendation.
Exact arithmetic synthesis is allowed when all operands are bound and share an
authorized comparison or synthesis basis.
When one block compares two or more materials under a shared ranking_scope and uses
the ranking_measure, bind ranking_position_measure for every compared item and keep
the prose in the exact declared ranking_order and priority_rank_order.
Preserve every declared count partition exactly. Treat a structurally absent group as
zero only when its interpretation contract authorizes zero fill through complete query
coverage and passed reconciliation. Translate material fact names and field identifiers
into business language; never publish handles, enums, or snake_case machine vocabulary.
Bind every explicit number, date, amount, range, ratio, score, and rank used by a
repaired block; projection membership without a block binding is insufficient.
Treat bound numeric values as exact unless their fact contract declares a range or
approximation. Every qualitative numeric modifier must follow the bound value,
threshold, range, and interpretation contract exactly. When typed limitation semantics
declare source_availability=available and evidence_role=background_context, describe
the source as available background or localization evidence and preserve the restriction
on direct attribution or causal conclusions. Do not describe it as missing, unavailable,
incomplete, or a data contract gap without an explicit typed completeness fact.
"""
_VERIFIER_SYSTEM_PROMPT = """\
Independently evaluate each supplied narrative block against the public material
projection. Check publication ceilings, claim-to-material membership, exact facts in
evidence_materials, recommendations, limitation-to-boundary_facet membership, and the
block's declared handle-only fact bindings. Resolve fact details from evidence_materials;
the block cannot restate or override them. Enforce every evidence interpretation_contract
and synthesis_contract: reject cross-slice addition, contribution wording, or ranking on
an undeclared basis; require the declared synthesis fact group to remain complete. Check
every multi-item ranking against its bound ranking_measure and
ranking_position_measure, and veto any omitted, renumbered, or reordered item. Check
declared count partitions and veto any prose that nests disjoint parts, changes their
whole, or calls an authorized reconciled zero missing data. Veto exact material fact
names, field identifiers, handles, enums, or snake_case machine vocabulary copied into
customer prose.
Veto every explicit number, date, amount, range, ratio, score, or rank that lacks an
exact material_fact_binding on that block, even when the value exists elsewhere in the
projection.
Veto a qualitative numeric modifier that is not logically entailed by the exact bound
value, threshold, range, or interpretation contract. Use numeric_qualifier_mismatch as
the exact reason_code. A bound ratio of 0.5 cannot be called above, below, near, or
slightly different from 50%.
When typed limitation semantics declare source_availability=available and
evidence_role=background_context, veto prose that calls the source missing,
unavailable, incomplete, or a data contract gap without an explicit typed completeness
fact. Require the prose to preserve the declared restriction on direct attribution or
causal conclusions. Use evidence_role_wording_mismatch as the exact reason_code.
typed recommendation commitments predicate by predicate, rejecting any prose that raises
an action stage, changes its domain, or strengthens a hypothesis/conditional outcome into
an expected business effect. Check boundary scope precisely: result_group_count or
window_aggregate counts are not raw sample sizes, local_claim_family limitations cannot
degrade unrelated claims or the whole analysis, and supported_with_limitations is not an
evidence contradiction. Conflict language requires explicit contradicted semantics.
Check customer prose against answer_context.locale. Veto avoidable mixed-language prose
or untranslated ordinary source-language terms; proper nouns, customer-safe business
labels, and conventional abbreviations may remain. Check every named recommendation
subject against its own typed diagnostic premises. Veto wording that assigns a positive
subject a negative-remediation purpose, assigns a negative subject a replication or
scaling purpose, or collapses different subject directions into an ambiguous shared
action. Use customer_locale_language_inconsistent,
recommendation_subject_direction_mismatch, or
recommendation_subject_direction_ambiguous as the exact reason_code for these quality
findings. Publication remains governed by the separate non-blocking customer projection
policy.
Allow original explanation, prioritization, and exact arithmetic synthesis when every
operand is bound and the declared comparison or synthesis basis authorizes it. Preserve
the writer's freedom to synthesize and explain when those authority boundaries hold. You
may only accept a block or veto it. Do not draft, suggest, or return replacement prose.

requirement_limitation_scope is the authoritative projection of user-requested
publication obligations into this verifier call. For each requirement it provides the
claim options, required limitations, coverage semantics, assertion scope, typed binding
topology, and the exact claim and limitation coverage of every relevant target or context
block. Enforce binding_mode exactly. A claim_or_boundary limitation may qualify only one
of its claim_binding_options or a boundary block. A boundary_only limitation must remain
a boundary on its concrete unavailable analysis path and must not weaken an accepted
claim. Typed identity and outcome provenance must be rendered as the concrete business
analysis path when available. Veto vague availability or trust-boundary wording that
omits that typed path. Do not require mechanical repetition of boundary_code, capability
identifiers, retryability, provider fields, or other internal metadata; verify their
business meaning instead.
missing_required_limitation_handles records requirement-level boundaries that this block
does not bind. Missing coverage alone is not a block veto because required coverage may
be distributed across blocks. Use it when testing whether prose overstates completeness,
scope, or consistency. When a block binds a required limitation, its prose must express
that limitation's own boundary facets; another limitation cannot stand in for it.

verification_scope declares whether this is a full verification or a focused retry. In
full mode, independently evaluate every block and return one decision per target_block_id.
In focused mode, context_blocks are byte-for-byte unchanged blocks whose accepted verdict
was settled by the referenced source verifier report under the same material projection
and verifier prompt version. Read them when checking cross-block consistency, but do not
return decisions for them. Evaluate only blocks listed in target_block_ids. If a target
conflicts with settled context, veto the target and identify handles bound to that target.
Return one JSON object with exactly this shape: {"decisions":[{"block_id":string,
"disposition":"accepted"|"vetoed","reason_code":string|null,
"affected_claim_handles":[string],"affected_recommendation_handles":[string],
"limitation_handles":[string]}]}. Return exactly one decision for every supplied
block. Accepted decisions have a null reason_code and empty handle lists. A veto has a
non-empty reason_code and identifies at least one handle already bound to that block.
"""


class TypedNarrativeLLM(Protocol):
    def invoke_json(
        self,
        *,
        task: str,
        prompt_version: str,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        output_validator: Callable[[Mapping[str, Any]], None] | None = None,
        model_tier: str = "default",
        thinking: str | None = None,
    ) -> Any: ...


class SensitiveOutputInspector(Protocol):
    def __call__(
        self,
        *,
        narrative: NarrativeDocument,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> Sequence[SensitiveOutputFinding]: ...


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NarrativeWorkflowError(error)
    return value


def _raw_text(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NarrativeWorkflowError(error)
    return value


def _positive_integer(value: Any, error: str) -> int:
    if type(value) is not int or value < 1:
        raise NarrativeWorkflowError(error)
    return value


def _ordered_string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise NarrativeWorkflowError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if (not allow_empty and not normalized) or len(normalized) != len(set(normalized)):
        raise NarrativeWorkflowError(error)
    return normalized


def _sorted_string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    return tuple(sorted(_ordered_string_tuple(value, error, allow_empty=allow_empty)))


def _mapping_sequence(value: Any, error: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise NarrativeWorkflowError(error)
    if any(not isinstance(item, Mapping) for item in value):
        raise NarrativeWorkflowError(error)
    return tuple(value)


def _strict_mapping(
    value: Any,
    fields: frozenset[str],
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise NarrativeWorkflowError(error)
    return value


def _strict_record_payload(
    value: Any,
    record_type: type,
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(
        record_type.__dataclass_fields__
    ):
        raise NarrativeWorkflowError(error)
    return value


def _freeze(value: Any, error: str) -> Any:
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise NarrativeWorkflowError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item, error) for item in normalized)
    return normalized


def _typed_sequence(
    value: Any,
    record_type: type,
    identity_field: str,
    error: str,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise NarrativeWorkflowError(error)
    records = tuple(value)
    if any(type(item) is not record_type for item in records):
        raise NarrativeWorkflowError(error)
    identities = tuple(str(getattr(item, identity_field)) for item in records)
    if len(identities) != len(set(identities)):
        raise NarrativeWorkflowError(error)
    return tuple(sorted(records, key=lambda item: str(getattr(item, identity_field))))


def _validated_settlement(value: ClaimSettlement) -> ClaimSettlement:
    try:
        return validate_typed_claim_settlement(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeWorkflowError("narrative_claim_settlement_invalid") from exc


def _assert_bundle_settlement_closure(
    authority_bundle: AuthorityBundle,
    settlement: ClaimSettlement,
) -> None:
    if type(authority_bundle) is not AuthorityBundle:
        raise NarrativeWorkflowError("narrative_authority_bundle_invalid")
    graph = settlement.claim_graph
    expected_coverage_refs = tuple(
        item.coverage_ref for item in graph.obligation_coverage
    )
    expected_claim_refs = tuple(item.claim_ref for item in settlement.accepted_claims)
    if (
        authority_bundle.seal_state != "sealed"
        or authority_bundle.authority_namespace_ref
        != settlement.authority_namespace_ref
        or authority_bundle.run_attempt_id
        != settlement.authority_namespace.run_attempt_id
        or authority_bundle.claim_settlement_ref != settlement.settlement_ref
        or authority_bundle.claim_settlement_digest != settlement.content_digest
        or authority_bundle.claim_graph_ref != graph.claim_graph_ref
        or authority_bundle.claim_graph_digest != graph.content_digest
        or authority_bundle.claim_verifier_report_ref
        != settlement.verifier_report.verifier_report_ref
        or authority_bundle.authority_mode != graph.authority_mode
        or authority_bundle.obligation_coverage_refs != expected_coverage_refs
        or authority_bundle.verified_claim_refs != expected_claim_refs
        or authority_bundle.evidence_refs != tuple(graph.evidence_ceiling_by_ref)
    ):
        raise NarrativeWorkflowError("narrative_authority_closure_invalid")


@dataclass(frozen=True)
class NarrativeAnswerContext:
    context_ref: str
    user_question: str
    answer_goal: str
    locale: str
    business_context: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        user_question: str,
        answer_goal: str,
        locale: str,
        business_context: Sequence[str],
    ) -> "NarrativeAnswerContext":
        body = {
            "user_question": _raw_text(
                user_question, "narrative_answer_context_question_invalid"
            ),
            "answer_goal": _raw_text(
                answer_goal, "narrative_answer_context_goal_invalid"
            ),
            "locale": _required_string(
                locale, "narrative_answer_context_locale_invalid"
            ),
            "business_context": _ordered_string_tuple(
                business_context,
                "narrative_answer_context_business_context_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            context_ref="narrative-answer-context:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NarrativeAnswerContext":
        payload = _strict_record_payload(
            payload,
            cls,
            "narrative_answer_context_shape_invalid",
        )
        rebuilt = cls.create(
            user_question=payload["user_question"],
            answer_goal=payload["answer_goal"],
            locale=payload["locale"],
            business_context=payload["business_context"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeWorkflowError("narrative_answer_context_integrity_invalid")
        return rebuilt

    def to_writer_payload(self) -> dict[str, Any]:
        return {
            "answer_goal": self.answer_goal,
            "locale": self.locale,
            "business_context": list(self.business_context),
        }


@dataclass(frozen=True)
class ReviewedPublicFactMaterialization:
    materialization_ref: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    claim_settlement_ref: str
    claim_settlement_digest: str
    review_ref: str
    reviewed_by: str
    public_facts: tuple[PublicFactDescriptor, ...]
    public_limitations: tuple[PublicLimitation, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_bundle: AuthorityBundle,
        claim_settlement: ClaimSettlement,
        review_ref: str,
        reviewed_by: str,
        public_facts: Sequence[PublicFactDescriptor],
        public_limitations: Sequence[PublicLimitation],
    ) -> "ReviewedPublicFactMaterialization":
        settlement = _validated_settlement(claim_settlement)
        _assert_bundle_settlement_closure(authority_bundle, settlement)
        facts = _typed_sequence(
            public_facts,
            PublicFactDescriptor,
            "fact_ref",
            "reviewed_public_fact_materialization_facts_invalid",
        )
        limitations = _typed_sequence(
            public_limitations,
            PublicLimitation,
            "limitation_ref",
            "reviewed_public_fact_materialization_limitations_invalid",
        )
        claims_by_ref = {item.claim_ref: item for item in settlement.accepted_claims}
        if {item.claim_ref for item in facts} != set(claims_by_ref):
            raise NarrativeWorkflowError(
                "reviewed_public_fact_materialization_claim_coverage_invalid"
            )
        for fact in facts:
            claim = claims_by_ref.get(fact.claim_ref)
            if claim is None:
                raise NarrativeWorkflowError(
                    "reviewed_public_fact_materialization_claim_coverage_invalid"
                )
            try:
                PublicFactDescriptor.from_dict(fact.to_dict(), claim=claim)
            except (TypeError, ValueError) as exc:
                raise NarrativeWorkflowError(
                    "reviewed_public_fact_materialization_fact_integrity_invalid"
                ) from exc
        for limitation in limitations:
            try:
                PublicLimitation.from_dict(limitation.to_dict())
            except (TypeError, ValueError) as exc:
                raise NarrativeWorkflowError(
                    "reviewed_public_fact_materialization_limitation_integrity_invalid"
                ) from exc
        expected_limitations = set(authority_bundle.limitation_refs)
        if {item.limitation_ref for item in limitations} != expected_limitations:
            raise NarrativeWorkflowError(
                "reviewed_public_fact_materialization_limitation_closure_invalid"
            )
        body = {
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "claim_settlement_ref": settlement.settlement_ref,
            "claim_settlement_digest": settlement.content_digest,
            "review_ref": _required_string(
                review_ref, "reviewed_public_fact_materialization_review_ref_invalid"
            ),
            "reviewed_by": _required_string(
                reviewed_by,
                "reviewed_public_fact_materialization_reviewer_invalid",
            ),
            "public_facts": facts,
            "public_limitations": limitations,
        }
        digest = canonical_digest(body)
        return cls(
            materialization_ref="reviewed-public-materialization:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        claim_settlement: ClaimSettlement,
    ) -> "ReviewedPublicFactMaterialization":
        payload = _strict_record_payload(
            payload,
            cls,
            "reviewed_public_fact_materialization_shape_invalid",
        )
        settlement = _validated_settlement(claim_settlement)
        claims_by_ref = {item.claim_ref: item for item in settlement.accepted_claims}
        raw_facts = _mapping_sequence(
            payload["public_facts"],
            "reviewed_public_fact_materialization_facts_invalid",
        )
        try:
            facts = tuple(
                PublicFactDescriptor.from_dict(
                    item,
                    claim=claims_by_ref[item["claim_ref"]],
                )
                for item in raw_facts
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NarrativeWorkflowError(
                "reviewed_public_fact_materialization_fact_integrity_invalid"
            ) from exc
        limitations = tuple(
            PublicLimitation.from_dict(item)
            for item in _mapping_sequence(
                payload["public_limitations"],
                "reviewed_public_fact_materialization_limitations_invalid",
            )
        )
        rebuilt = cls.create(
            authority_bundle=authority_bundle,
            claim_settlement=settlement,
            review_ref=payload["review_ref"],
            reviewed_by=payload["reviewed_by"],
            public_facts=facts,
            public_limitations=limitations,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeWorkflowError(
                "reviewed_public_fact_materialization_integrity_invalid"
            )
        return rebuilt


@dataclass(frozen=True)
class NarrativeProviderCallInput:
    call_input_ref: str
    purpose: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    material_projection_ref: str
    material_projection_digest: str
    payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        purpose: str,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
        payload: Mapping[str, Any],
    ) -> "NarrativeProviderCallInput":
        if purpose not in _PROVIDER_PURPOSES:
            raise NarrativeWorkflowError("narrative_provider_purpose_invalid")
        if not isinstance(payload, Mapping):
            raise NarrativeWorkflowError("narrative_provider_payload_invalid")
        if type(material_projection) is not NarrativeMaterialProjection:
            raise NarrativeWorkflowError(
                "narrative_provider_material_projection_invalid"
            )
        material_projection.assert_integrity()
        normalized_payload = canonical_value(payload)
        body = {
            "purpose": purpose,
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "material_projection_ref": material_projection.projection_ref,
            "material_projection_digest": material_projection.content_digest,
            "payload": _freeze(
                normalized_payload, "narrative_provider_payload_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            call_input_ref="narrative-provider-input:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        material_projection: NarrativeMaterialProjection,
    ) -> "NarrativeProviderCallInput":
        payload = _strict_record_payload(
            payload,
            cls,
            "narrative_provider_call_input_shape_invalid",
        )
        rebuilt = cls.create(
            purpose=payload["purpose"],
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            payload=payload["payload"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeWorkflowError(
                "narrative_provider_call_input_integrity_invalid"
            )
        return rebuilt

    def to_provider_payload(self) -> dict[str, Any]:
        return canonical_value(self.payload)


@dataclass(frozen=True)
class NarrativeProviderCallAudit:
    audit_ref: str
    call_input_ref: str
    purpose: str
    provider_ref: str
    model_ref: str
    attempt_count: int
    provider_response_refs: tuple[str, ...]
    output_digest: str
    audit_payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        call_input: NarrativeProviderCallInput,
        output: Mapping[str, Any],
        audit: Mapping[str, Any],
        responses: Sequence[RestrictedProviderResponse],
    ) -> "NarrativeProviderCallAudit":
        if not isinstance(audit, Mapping):
            raise NarrativeWorkflowError("narrative_provider_audit_invalid")
        provider_ref = _required_string(
            audit.get("provider"), "narrative_provider_audit_provider_invalid"
        )
        model_ref = _required_string(
            audit.get("model"), "narrative_provider_audit_model_invalid"
        )
        attempt_count = _positive_integer(
            audit.get("attempt_count"),
            "narrative_provider_audit_attempt_count_invalid",
        )
        normalized_responses = tuple(responses)
        if (
            not normalized_responses
            or any(
                type(item) is not RestrictedProviderResponse
                for item in normalized_responses
            )
            or normalized_responses[-1].attempt_number != attempt_count
            or any(item.purpose != call_input.purpose for item in normalized_responses)
            or any(item.provider_ref != provider_ref for item in normalized_responses)
            or any(item.model_ref != model_ref for item in normalized_responses)
            or any(
                item.input_ref != call_input.call_input_ref
                for item in normalized_responses
            )
            or any(
                item.input_digest != call_input.content_digest
                for item in normalized_responses
            )
        ):
            raise NarrativeWorkflowError(
                "narrative_provider_audit_response_closure_invalid"
            )
        body = {
            "call_input_ref": call_input.call_input_ref,
            "purpose": call_input.purpose,
            "provider_ref": provider_ref,
            "model_ref": model_ref,
            "attempt_count": attempt_count,
            "provider_response_refs": tuple(
                item.response_ref for item in normalized_responses
            ),
            "output_digest": canonical_digest(output),
            "audit_payload": _freeze(audit, "narrative_provider_audit_payload_invalid"),
        }
        digest = canonical_digest(body)
        return cls(
            audit_ref="narrative-provider-audit:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        call_inputs_by_ref: Mapping[str, NarrativeProviderCallInput],
        responses_by_ref: Mapping[str, RestrictedProviderResponse],
    ) -> "NarrativeProviderCallAudit":
        payload = _strict_record_payload(
            payload,
            cls,
            "narrative_provider_audit_shape_invalid",
        )
        try:
            call_input = call_inputs_by_ref[payload["call_input_ref"]]
            responses = tuple(
                responses_by_ref[item] for item in payload["provider_response_refs"]
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeWorkflowError(
                "narrative_provider_audit_child_closure_invalid"
            ) from exc
        raw_audit = payload["audit_payload"]
        if not isinstance(raw_audit, Mapping):
            raise NarrativeWorkflowError("narrative_provider_audit_payload_invalid")
        structured_output = raw_audit.get("structured_output")
        if not isinstance(structured_output, Mapping):
            raise NarrativeWorkflowError(
                "narrative_provider_audit_structured_output_invalid"
            )
        replayed_responses = _responses_from_audit(
            call_input=call_input,
            output=structured_output,
            audit=raw_audit,
        )
        if replayed_responses != responses:
            raise NarrativeWorkflowError(
                "narrative_provider_audit_response_closure_invalid"
            )
        rebuilt = cls.create(
            call_input=call_input,
            output=structured_output,
            audit=raw_audit,
            responses=responses,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeWorkflowError("narrative_provider_audit_integrity_invalid")
        return rebuilt


@dataclass(frozen=True)
class FocusedNarrativeRetry:
    retry_ref: str
    source_narrative_id: str
    rejection_report_ref: str
    targeted_block_ids: tuple[str, ...]
    preserved_source_block_ids: tuple[str, ...]
    writer_attempt_ref: str
    resulting_narrative_id: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        source_narrative: NarrativeDocument,
        rejection_report: BlockVerifierReport,
        material_projection: NarrativeMaterialProjection,
        targeted_blocks: Sequence[NarrativeBlock],
        preserved_blocks: Sequence[NarrativeBlock],
        writer_attempt: NarrativeWriterAttempt,
        resulting_narrative: NarrativeDocument,
    ) -> "FocusedNarrativeRetry":
        targets = tuple(item.block_id for item in targeted_blocks)
        preserved = tuple(item.block_id for item in preserved_blocks)
        retry_plan = _compile_focused_retry_plan(
            source_order=source_narrative.blocks,
            accepted_block_ids=rejection_report.accepted_block_ids,
            rejected_block_ids=rejection_report.rejected_block_ids,
            material_projection=material_projection,
        )
        expected_targets = tuple(
            item.block_id for item in retry_plan.source_target_blocks
        )
        expected_preserved = tuple(
            item.block_id for item in retry_plan.preserved_blocks
        )
        if (
            not targets
            or rejection_report.narrative_id != source_narrative.narrative_id
            or targets != expected_targets
            or preserved != expected_preserved
            or resulting_narrative.writer_attempt_id != writer_attempt.attempt_id
        ):
            raise NarrativeWorkflowError("focused_narrative_retry_closure_invalid")
        body = {
            "source_narrative_id": source_narrative.narrative_id,
            "rejection_report_ref": rejection_report.verifier_report_ref,
            "targeted_block_ids": targets,
            "preserved_source_block_ids": preserved,
            "writer_attempt_ref": writer_attempt.writer_attempt_ref,
            "resulting_narrative_id": resulting_narrative.narrative_id,
        }
        digest = canonical_digest(body)
        return cls(
            retry_ref="focused-narrative-retry:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        narratives_by_id: Mapping[str, NarrativeDocument],
        verifier_reports_by_ref: Mapping[str, BlockVerifierReport],
        writer_attempts_by_ref: Mapping[str, NarrativeWriterAttempt],
        material_projection: NarrativeMaterialProjection,
    ) -> "FocusedNarrativeRetry":
        payload = _strict_record_payload(
            payload,
            cls,
            "focused_narrative_retry_shape_invalid",
        )
        try:
            source = narratives_by_id[payload["source_narrative_id"]]
            report = verifier_reports_by_ref[payload["rejection_report_ref"]]
            writer_attempt = writer_attempts_by_ref[payload["writer_attempt_ref"]]
            resulting = narratives_by_id[payload["resulting_narrative_id"]]
            source_blocks = {item.block_id: item for item in source.blocks}
            targeted = tuple(
                source_blocks[item] for item in payload["targeted_block_ids"]
            )
            preserved = tuple(
                source_blocks[item] for item in payload["preserved_source_block_ids"]
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeWorkflowError(
                "focused_narrative_retry_child_closure_invalid"
            ) from exc
        rebuilt = cls.create(
            source_narrative=source,
            rejection_report=report,
            material_projection=material_projection,
            targeted_blocks=targeted,
            preserved_blocks=preserved,
            writer_attempt=writer_attempt,
            resulting_narrative=resulting,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeWorkflowError("focused_narrative_retry_integrity_invalid")
        return rebuilt


@dataclass(frozen=True)
class NarrativeWorkflowResult:
    authority_bundle_ref: str
    authority_bundle_digest: str
    claim_settlement_ref: str
    claim_settlement_digest: str
    public_materialization: ReviewedPublicFactMaterialization
    visibility_policy: PublicationFieldVisibilityPolicy
    answer_context: NarrativeAnswerContext
    material_projection: NarrativeMaterialProjection
    provider_call_inputs: tuple[NarrativeProviderCallInput, ...]
    provider_responses: tuple[RestrictedProviderResponse, ...]
    provider_audits: tuple[NarrativeProviderCallAudit, ...]
    writer_attempts: tuple[NarrativeWriterAttempt, ...]
    narratives: tuple[NarrativeDocument, ...]
    local_reports: tuple[BlockLocalValidationReport, ...]
    verification_attempts: tuple[BlockVerificationAttempt, ...]
    verifier_reports: tuple[BlockVerifierReport, ...]
    focused_retry: FocusedNarrativeRetry | None
    final_accepted_narrative: NarrativeDocument | None
    final_local_report: BlockLocalValidationReport
    projection_ready_verifier_report: BlockVerifierReport
    publication_ready: bool
    withheld_required_block_ids: tuple[str, ...]
    content_digest: str

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    @property
    def writer_contract_findings(self) -> tuple[str, ...]:
        if not self.provider_audits:
            return ()
        raw_findings = self.provider_audits[0].audit_payload.get(
            _WRITER_CONTRACT_FINDINGS_AUDIT_FIELD,
        )
        if isinstance(raw_findings, (str, bytes)) or not isinstance(
            raw_findings, Sequence
        ):
            raise NarrativeWorkflowError("narrative_writer_contract_findings_invalid")
        findings = tuple(
            _required_string(
                item,
                "narrative_writer_contract_findings_invalid",
            )
            for item in raw_findings
        )
        if len(findings) != len(set(findings)):
            raise NarrativeWorkflowError("narrative_writer_contract_findings_invalid")
        return findings

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        claim_settlement: ClaimSettlement,
        evidence_entries: Sequence[EvidenceLedgerEntry],
        recommendations: Sequence[RecommendationRecord],
    ) -> "NarrativeWorkflowResult":
        payload = _strict_record_payload(
            payload,
            cls,
            "narrative_workflow_result_shape_invalid",
        )
        settlement = _validated_settlement(claim_settlement)
        _assert_bundle_settlement_closure(authority_bundle, settlement)
        raw_materialization = payload["public_materialization"]
        raw_policy = payload["visibility_policy"]
        raw_context = payload["answer_context"]
        raw_material_projection = payload["material_projection"]
        if not all(
            isinstance(item, Mapping)
            for item in (
                raw_materialization,
                raw_policy,
                raw_context,
                raw_material_projection,
            )
        ):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_authority_children_invalid"
            )
        materialization = ReviewedPublicFactMaterialization.from_dict(
            raw_materialization,
            authority_bundle=authority_bundle,
            claim_settlement=settlement,
        )
        try:
            policy = PublicationFieldVisibilityPolicy.from_dict(raw_policy)
        except (TypeError, ValueError) as exc:
            raise NarrativeWorkflowError(
                "narrative_workflow_result_visibility_policy_invalid"
            ) from exc
        context = NarrativeAnswerContext.from_dict(raw_context)
        normalized_recommendations = _typed_sequence(
            recommendations,
            RecommendationRecord,
            "recommendation_ref",
            "narrative_workflow_result_recommendations_invalid",
        )
        try:
            palette = PublicClaimPalette.derive(
                authority_bundle=authority_bundle,
                claims=settlement.accepted_claims,
                claim_keys=settlement.accepted_claim_keys,
                recommendations=normalized_recommendations,
                public_facts=materialization.public_facts,
                public_limitations=materialization.public_limitations,
                visibility_policy=policy,
            )
            material_projection = NarrativeMaterialProjection.from_dict(
                raw_material_projection,
                palette=palette,
                claim_settlement=settlement,
                evidence_entries=evidence_entries,
            )
        except (TypeError, ValueError) as exc:
            raise NarrativeWorkflowError(
                "narrative_workflow_result_material_projection_invalid"
            ) from exc

        call_inputs = tuple(
            NarrativeProviderCallInput.from_dict(
                item,
                authority_bundle=authority_bundle,
                material_projection=material_projection,
            )
            for item in _mapping_sequence(
                payload["provider_call_inputs"],
                "narrative_workflow_result_call_inputs_invalid",
            )
        )
        call_inputs_by_ref = {item.call_input_ref: item for item in call_inputs}
        if len(call_inputs_by_ref) != len(call_inputs):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_call_inputs_duplicated"
            )
        responses = tuple(
            RestrictedProviderResponse.from_dict(item)
            for item in _mapping_sequence(
                payload["provider_responses"],
                "narrative_workflow_result_responses_invalid",
            )
        )
        responses_by_ref = {item.response_ref: item for item in responses}
        if len(responses_by_ref) != len(responses):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_responses_duplicated"
            )
        audits = tuple(
            NarrativeProviderCallAudit.from_dict(
                item,
                call_inputs_by_ref=call_inputs_by_ref,
                responses_by_ref=responses_by_ref,
            )
            for item in _mapping_sequence(
                payload["provider_audits"],
                "narrative_workflow_result_audits_invalid",
            )
        )
        if len({item.audit_ref for item in audits}) != len(audits):
            raise NarrativeWorkflowError("narrative_workflow_result_audits_duplicated")
        writer_attempts = tuple(
            NarrativeWriterAttempt.from_dict(item)
            for item in _mapping_sequence(
                payload["writer_attempts"],
                "narrative_workflow_result_writer_attempts_invalid",
            )
        )
        writer_attempts_by_ref = {
            item.writer_attempt_ref: item for item in writer_attempts
        }
        if len(writer_attempts_by_ref) != len(writer_attempts):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_writer_attempts_duplicated"
            )
        for attempt in writer_attempts:
            response = responses_by_ref.get(attempt.provider_response_ref)
            if response != attempt.provider_response:
                raise NarrativeWorkflowError(
                    "narrative_workflow_result_writer_response_closure_invalid"
                )
        narratives = tuple(
            NarrativeDocument.from_dict(item)
            for item in _mapping_sequence(
                payload["narratives"],
                "narrative_workflow_result_narratives_invalid",
            )
        )
        narratives_by_id = {item.narrative_id: item for item in narratives}
        if len(narratives_by_id) != len(narratives):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_narratives_duplicated"
            )
        for narrative in narratives:
            attempt = writer_attempts_by_ref.get(
                narrative.writer_attempt.writer_attempt_ref
            )
            if attempt != narrative.writer_attempt:
                raise NarrativeWorkflowError(
                    "narrative_workflow_result_narrative_attempt_closure_invalid"
                )
        local_reports = []
        for item in _mapping_sequence(
            payload["local_reports"],
            "narrative_workflow_result_local_reports_invalid",
        ):
            try:
                narrative = narratives_by_id[item["narrative_id"]]
            except (KeyError, TypeError) as exc:
                raise NarrativeWorkflowError(
                    "narrative_workflow_result_local_report_narrative_invalid"
                ) from exc
            local_reports.append(
                BlockLocalValidationReport.from_dict(
                    item,
                    narrative=narrative,
                    material_projection=material_projection,
                    visibility_policy=policy,
                )
            )
        local_reports_tuple = tuple(local_reports)
        local_reports_by_ref = {
            item.local_report_ref: item for item in local_reports_tuple
        }
        if len(local_reports_by_ref) != len(local_reports_tuple):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_local_reports_duplicated"
            )
        verification_attempts = []
        for item in _mapping_sequence(
            payload["verification_attempts"],
            "narrative_workflow_result_verification_attempts_invalid",
        ):
            try:
                narrative = narratives_by_id[item["narrative_id"]]
                local_report = local_reports_by_ref[item["local_report_ref"]]
            except (KeyError, TypeError) as exc:
                raise NarrativeWorkflowError(
                    "narrative_workflow_result_verification_children_invalid"
                ) from exc
            attempt = BlockVerificationAttempt.from_dict(
                item,
                narrative=narrative,
                local_report=local_report,
            )
            response = responses_by_ref.get(attempt.provider_response_ref)
            if response != attempt.provider_response:
                raise NarrativeWorkflowError(
                    "narrative_workflow_result_verifier_response_closure_invalid"
                )
            verification_attempts.append(attempt)
        verification_attempts_tuple = tuple(verification_attempts)
        verification_attempts_by_ref = {
            item.verification_attempt_ref: item for item in verification_attempts_tuple
        }
        if len(verification_attempts_by_ref) != len(verification_attempts_tuple):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_verification_attempts_duplicated"
            )
        verifier_reports = []
        for item in _mapping_sequence(
            payload["verifier_reports"],
            "narrative_workflow_result_verifier_reports_invalid",
        ):
            try:
                narrative = narratives_by_id[item["narrative_id"]]
                local_report = local_reports_by_ref[item["local_report_ref"]]
            except (KeyError, TypeError) as exc:
                raise NarrativeWorkflowError(
                    "narrative_workflow_result_verifier_children_invalid"
                ) from exc
            report = BlockVerifierReport.from_dict(
                item,
                narrative=narrative,
                material_projection=material_projection,
                visibility_policy=policy,
                local_report=local_report,
            )
            attempt = verification_attempts_by_ref.get(report.verification_attempt_ref)
            if attempt != report.verification_attempt:
                raise NarrativeWorkflowError(
                    "narrative_workflow_result_verifier_attempt_closure_invalid"
                )
            verifier_reports.append(report)
        verifier_reports_tuple = tuple(verifier_reports)
        verifier_reports_by_ref = {
            item.verifier_report_ref: item for item in verifier_reports_tuple
        }
        if len(verifier_reports_by_ref) != len(verifier_reports_tuple):
            raise NarrativeWorkflowError(
                "narrative_workflow_result_verifier_reports_duplicated"
            )
        raw_focused_retry = payload["focused_retry"]
        if raw_focused_retry is None:
            focused_retry = None
        elif isinstance(raw_focused_retry, Mapping):
            focused_retry = FocusedNarrativeRetry.from_dict(
                raw_focused_retry,
                narratives_by_id=narratives_by_id,
                verifier_reports_by_ref=verifier_reports_by_ref,
                writer_attempts_by_ref=writer_attempts_by_ref,
                material_projection=material_projection,
            )
        else:
            raise NarrativeWorkflowError(
                "narrative_workflow_result_focused_retry_invalid"
            )
        rebuilt = _workflow_result(
            authority_bundle=authority_bundle,
            claim_settlement=settlement,
            public_materialization=materialization,
            visibility_policy=policy,
            answer_context=context,
            material_projection=material_projection,
            provider_call_inputs=call_inputs,
            provider_responses=responses,
            provider_audits=audits,
            writer_attempts=writer_attempts,
            narratives=narratives,
            local_reports=local_reports_tuple,
            verification_attempts=verification_attempts_tuple,
            verifier_reports=verifier_reports_tuple,
            focused_retry=focused_retry,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeWorkflowError("narrative_workflow_result_integrity_invalid")
        return rebuilt

    def replay(
        self,
        *,
        authority_bundle: AuthorityBundle,
        claim_settlement: ClaimSettlement,
        evidence_entries: Sequence[EvidenceLedgerEntry],
        recommendations: Sequence[RecommendationRecord],
    ) -> "NarrativeWorkflowResult":
        return self.from_dict(
            self.to_dict(),
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            evidence_entries=evidence_entries,
            recommendations=recommendations,
        )


@dataclass(frozen=True)
class _ProviderInvocation:
    call_input: NarrativeProviderCallInput
    output: Mapping[str, Any]
    responses: tuple[RestrictedProviderResponse, ...]
    audit: NarrativeProviderCallAudit


def _provider_attempt_id(
    *,
    call_input: NarrativeProviderCallInput,
    provider_ref: str,
    model_ref: str,
    attempt_number: int,
    response_id: str,
) -> str:
    digest = canonical_digest(
        {
            "call_input_ref": call_input.call_input_ref,
            "purpose": call_input.purpose,
            "provider_ref": provider_ref,
            "model_ref": model_ref,
            "attempt_number": attempt_number,
            "response_id": response_id,
        }
    )
    return "narrative-provider-attempt:sha256:" + digest


def _responses_from_audit(
    *,
    call_input: NarrativeProviderCallInput,
    output: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> tuple[RestrictedProviderResponse, ...]:
    provider_ref = _required_string(
        audit.get("provider"), "narrative_provider_audit_provider_invalid"
    )
    model_ref = _required_string(
        audit.get("model"), "narrative_provider_audit_model_invalid"
    )
    final_attempt = _positive_integer(
        audit.get("attempt_count"),
        "narrative_provider_audit_attempt_count_invalid",
    )
    structured_output = audit.get("structured_output")
    if not isinstance(structured_output, Mapping) or canonical_value(
        structured_output
    ) != canonical_value(output):
        raise NarrativeWorkflowError(
            "narrative_provider_audit_structured_output_invalid"
        )
    raw_final = _raw_text(
        audit.get("raw_response_content"),
        "narrative_provider_audit_raw_response_invalid",
    )
    try:
        parsed_final = parse_llm_structured_response_content(raw_final)
    except ValueError as exc:
        raise NarrativeWorkflowError(
            "narrative_provider_audit_raw_response_invalid"
        ) from exc
    if canonical_value(parsed_final) != canonical_value(output):
        raise NarrativeWorkflowError("narrative_provider_audit_output_closure_invalid")
    raw_failures = audit.get("attempt_failures", ())
    if isinstance(raw_failures, (str, bytes)) or not isinstance(raw_failures, Sequence):
        raise NarrativeWorkflowError(
            "narrative_provider_audit_attempt_failures_invalid"
        )
    attempts: list[tuple[int, str, str]] = []
    for failure in raw_failures:
        if not isinstance(failure, Mapping):
            raise NarrativeWorkflowError(
                "narrative_provider_audit_attempt_failures_invalid"
            )
        raw_content = failure.get("raw_response_content")
        if raw_content is None:
            continue
        attempt_number = _positive_integer(
            failure.get("attempt"),
            "narrative_provider_audit_failure_attempt_invalid",
        )
        if attempt_number >= final_attempt:
            raise NarrativeWorkflowError(
                "narrative_provider_audit_failure_attempt_invalid"
            )
        attempts.append(
            (
                attempt_number,
                _raw_text(
                    raw_content,
                    "narrative_provider_audit_failure_content_invalid",
                ),
                str(failure.get("response_id") or ""),
            )
        )
    attempts.append((final_attempt, raw_final, str(audit.get("response_id") or "")))
    if len({item[0] for item in attempts}) != len(attempts):
        raise NarrativeWorkflowError(
            "narrative_provider_audit_attempt_identity_duplicated"
        )
    return tuple(
        RestrictedProviderResponse.create(
            attempt_id=_provider_attempt_id(
                call_input=call_input,
                provider_ref=provider_ref,
                model_ref=model_ref,
                attempt_number=attempt_number,
                response_id=response_id,
            ),
            purpose=call_input.purpose,
            provider_ref=provider_ref,
            model_ref=model_ref,
            input_ref=call_input.call_input_ref,
            input_digest=call_input.content_digest,
            attempt_number=attempt_number,
            content=content,
        )
        for attempt_number, content, response_id in sorted(attempts)
    )


def _invoke_provider(
    llm_client: TypedNarrativeLLM,
    *,
    call_input: NarrativeProviderCallInput,
    system_prompt: str,
    required_key: str,
    validator: Callable[[Mapping[str, Any]], None],
    output_normalizer: Callable[
        [Mapping[str, Any]], tuple[Mapping[str, Any], tuple[str, ...]]
    ]
    | None = None,
) -> _ProviderInvocation:
    def normalize_provider_output(
        output: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], tuple[str, ...]]:
        if output_normalizer is not None:
            return output_normalizer(output)
        validator(output)
        return output, ()

    def provider_output_validator(output: Mapping[str, Any]) -> None:
        try:
            normalize_provider_output(output)
        except NarrativeWorkflowError as exc:
            raise LLMOutputError(str(exc)) from exc

    messages = (
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                call_input.to_provider_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    )
    outbound_bytes = len(
        json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if outbound_bytes > NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT:
        detail_digest = canonical_digest(
            {
                "call_input_ref": call_input.call_input_ref,
                "kind": "narrative_input_budget_exceeded",
                "outbound_bytes": outbound_bytes,
                "outbound_byte_limit": NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT,
            }
        )
        raise NarrativeProviderCallError(
            kind="narrative_input_budget_exceeded",
            retryability="not_retryable",
            call_input_ref=call_input.call_input_ref,
            technical_detail_ref="technical-detail:sha256:" + detail_digest,
        )
    try:
        result = llm_client.invoke_json(
            task="single_authority_" + call_input.purpose,
            prompt_version=_NARRATIVE_PROMPT_VERSION,
            messages=messages,
            required_keys=(required_key,),
            output_validator=provider_output_validator,
            model_tier="critical",
        )
    except (
        LLMConfigurationError,
        LLMOutputError,
        LLMProviderError,
        LLMTimeoutError,
    ) as exc:
        if isinstance(exc, LLMOutputError):
            kind = "provider_output_invalid"
            retryability = "not_retryable"
        elif isinstance(exc, LLMTimeoutError):
            kind = "provider_timeout"
            retryability = "retryable"
        elif isinstance(exc, LLMConfigurationError):
            kind = "provider_configuration_invalid"
            retryability = "not_retryable"
        elif isinstance(exc, LLMProviderError):
            kind = exc.kind
            retryability = exc.retryability
        else:
            raise AssertionError("typed_narrative_provider_error_unreachable")
        detail_digest = canonical_digest(
            {
                "call_input_ref": call_input.call_input_ref,
                "exception_type": type(exc).__name__,
                "kind": kind,
            }
        )
        raise NarrativeProviderCallError(
            kind=kind,
            retryability=retryability,
            call_input_ref=call_input.call_input_ref,
            technical_detail_ref="technical-detail:sha256:" + detail_digest,
        ) from exc
    raw_output = getattr(result, "output", None)
    raw_audit = getattr(result, "audit", None)
    if not isinstance(raw_output, Mapping) or not isinstance(raw_audit, Mapping):
        raise NarrativeWorkflowError("narrative_provider_result_invalid")
    audited_prompt_version = raw_audit.get("prompt_version")
    if (
        audited_prompt_version is not None
        and audited_prompt_version != _NARRATIVE_PROMPT_VERSION
    ):
        raise NarrativeWorkflowError("narrative_provider_audit_prompt_version_invalid")
    output, contract_findings = normalize_provider_output(raw_output)
    normalized_audit = {
        **raw_audit,
        "prompt_version": _NARRATIVE_PROMPT_VERSION,
        **(
            {_WRITER_CONTRACT_FINDINGS_AUDIT_FIELD: contract_findings}
            if output_normalizer is not None
            else {}
        ),
    }
    responses = _responses_from_audit(
        call_input=call_input,
        output=raw_output,
        audit=normalized_audit,
    )
    audit = NarrativeProviderCallAudit.create(
        call_input=call_input,
        output=raw_output,
        audit=normalized_audit,
        responses=responses,
    )
    return _ProviderInvocation(
        call_input=call_input,
        output=MappingProxyType(dict(output)),
        responses=responses,
        audit=audit,
    )


def _writer_block_shape(
    block: Mapping[str, Any],
    *,
    material_projection: NarrativeMaterialProjection,
) -> None:
    _strict_mapping(block, _WRITER_BLOCK_FIELDS, "narrative_writer_block_shape_invalid")
    if block["role"] not in NARRATIVE_BLOCK_ROLES:
        raise NarrativeWorkflowError("narrative_writer_block_role_invalid")
    _raw_text(block["text"], "narrative_writer_block_text_invalid")
    claim_handles = _sorted_string_tuple(
        block["claim_handles"], "narrative_writer_claim_handles_invalid"
    )
    recommendation_handles = _sorted_string_tuple(
        block["recommendation_handles"],
        "narrative_writer_recommendation_handles_invalid",
    )
    limitation_handles = _sorted_string_tuple(
        block["limitation_handles"],
        "narrative_writer_limitation_handles_invalid",
    )
    if not narrative_block_authority_handles_are_valid(
        role=block["role"],
        claim_handles=claim_handles,
        recommendation_handles=recommendation_handles,
        limitation_handles=limitation_handles,
    ):
        raise NarrativeWorkflowError("narrative_writer_authority_handles_invalid")
    _required_string(block["statement_role"], "narrative_writer_statement_role_invalid")
    if type(block["required"]) is not bool:
        raise NarrativeWorkflowError("narrative_writer_required_invalid")
    resolved_bindings = tuple(
        _fact_binding_from_output(
            binding,
            material_projection=material_projection,
        )
        for binding in _mapping_sequence(
            block["material_fact_bindings"],
            "narrative_writer_fact_bindings_invalid",
        )
    )
    if len({item.binding_ref for item in resolved_bindings}) != len(resolved_bindings):
        raise NarrativeWorkflowError("narrative_writer_fact_bindings_duplicated")
    for resolved in resolved_bindings:
        if resolved.claim_handle not in set(claim_handles):
            raise NarrativeWorkflowError(
                "narrative_writer_fact_binding_block_claim_mismatch"
            )


def _publication_requirements_covered(
    *,
    material_projection: NarrativeMaterialProjection,
    claim_handles: frozenset[str],
    limitation_handles: frozenset[str],
) -> bool:
    for requirement in material_projection.publication_requirements:
        if requirement.status in {"satisfied", "mixed", "contradicted"}:
            if claim_handles.isdisjoint(requirement.claim_handles):
                return False
        elif requirement.status != "unavailable":
            raise NarrativeWorkflowError(
                "narrative_publication_requirement_status_invalid"
            )
        if not frozenset(requirement.limitation_handles).issubset(limitation_handles):
            return False
    return True


def _initial_writer_validator(
    output: Mapping[str, Any],
    *,
    authority_mode: str,
    material_projection: NarrativeMaterialProjection,
) -> None:
    _strict_mapping(
        output,
        frozenset({"blocks"}),
        "narrative_writer_output_shape_invalid",
    )
    blocks = _mapping_sequence(output["blocks"], "narrative_writer_blocks_invalid")
    if not blocks:
        raise NarrativeWorkflowError("narrative_writer_blocks_invalid")
    for block in blocks:
        _writer_block_shape(
            block,
            material_projection=material_projection,
        )
    required = tuple(block for block in blocks if block["required"] is True)
    if not required:
        raise NarrativeWorkflowError("narrative_writer_required_coverage_invalid")
    if authority_mode == "boundary_only":
        if (
            len(blocks) != 1
            or blocks[0]["role"] != "boundary"
            or blocks[0]["required"] is not True
            or blocks[0]["claim_handles"]
            or blocks[0]["recommendation_handles"]
            or blocks[0]["material_fact_bindings"]
            or not blocks[0]["limitation_handles"]
        ):
            raise NarrativeWorkflowError("narrative_writer_boundary_output_invalid")
    elif not any(block["claim_handles"] for block in required):
        raise NarrativeWorkflowError("narrative_writer_required_coverage_invalid")
    required_claim_handles = frozenset(
        handle for block in required for handle in block["claim_handles"]
    )
    required_limitation_handles = frozenset(
        handle for block in required for handle in block["limitation_handles"]
    )
    if not _publication_requirements_covered(
        material_projection=material_projection,
        claim_handles=required_claim_handles,
        limitation_handles=required_limitation_handles,
    ):
        raise NarrativeWorkflowError(
            "narrative_writer_publication_requirement_coverage_invalid"
        )


def _normalize_initial_writer_output_for_delivery(
    output: Mapping[str, Any],
    *,
    authority_mode: str,
    material_projection: NarrativeMaterialProjection,
) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    """Keep generated insight deliverable while auditing quality-only defects."""

    _strict_mapping(
        output,
        frozenset({"blocks"}),
        "narrative_writer_output_shape_invalid",
    )
    raw_blocks = _mapping_sequence(
        output["blocks"],
        "narrative_writer_blocks_invalid",
    )
    if not raw_blocks:
        raise NarrativeWorkflowError("narrative_writer_blocks_invalid")
    findings: list[str] = []
    normalized_blocks: list[dict[str, Any]] = []
    defaultable_fields = frozenset({"statement_role", "required"})
    for raw_block in raw_blocks:
        unknown_fields = set(raw_block) - set(_WRITER_BLOCK_FIELDS)
        missing_fields = set(_WRITER_BLOCK_FIELDS) - set(raw_block)
        if unknown_fields or not missing_fields.issubset(defaultable_fields):
            raise NarrativeWorkflowError("narrative_writer_block_shape_invalid")
        block = dict(raw_block)
        statement_role = block.get("statement_role")
        if (
            not isinstance(statement_role, str)
            or not statement_role.strip()
            or statement_role != statement_role.strip()
        ):
            block["statement_role"] = (
                block["role"]
                if isinstance(block.get("role"), str) and block["role"]
                else "business_reference"
            )
            findings.append("statement_role_defaulted")
        if type(block.get("required")) is not bool:
            block["required"] = True
            findings.append("required_flag_defaulted")
        _writer_block_shape(
            block,
            material_projection=material_projection,
        )
        normalized_blocks.append(block)

    if authority_mode == "boundary_only":
        block = normalized_blocks[0] if len(normalized_blocks) == 1 else None
        if (
            block is None
            or block["role"] != "boundary"
            or block["required"] is not True
            or block["claim_handles"]
            or block["recommendation_handles"]
            or block["material_fact_bindings"]
            or not block["limitation_handles"]
        ):
            raise NarrativeWorkflowError("narrative_writer_boundary_output_invalid")

    required_blocks = tuple(
        block for block in normalized_blocks if block["required"] is True
    )
    if authority_mode != "boundary_only" and (
        not required_blocks
        or not any(block["claim_handles"] for block in required_blocks)
    ):
        findings.append("required_block_coverage_incomplete")
    required_claim_handles = frozenset(
        handle for block in required_blocks for handle in block["claim_handles"]
    )
    required_limitation_handles = frozenset(
        handle for block in required_blocks for handle in block["limitation_handles"]
    )
    if not _publication_requirements_covered(
        material_projection=material_projection,
        claim_handles=required_claim_handles,
        limitation_handles=required_limitation_handles,
    ):
        findings.append("publication_requirement_coverage_incomplete")
    return (
        {"blocks": normalized_blocks},
        tuple(dict.fromkeys(findings)),
    )


def _fact_binding_from_output(
    payload: Mapping[str, Any],
    *,
    material_projection: NarrativeMaterialProjection,
) -> NarrativeFactBinding:
    _strict_mapping(
        payload,
        _FACT_BINDING_FIELDS,
        "narrative_writer_fact_binding_shape_invalid",
    )
    claim_handle = _required_string(
        payload["claim_handle"],
        "narrative_writer_fact_binding_claim_handle_invalid",
    )
    fact_handle = _required_string(
        payload["fact_handle"],
        "narrative_writer_fact_binding_fact_handle_invalid",
    )
    claim = next(
        (
            item
            for item in material_projection.claims
            if item.claim_handle == claim_handle
        ),
        None,
    )
    if claim is None:
        raise NarrativeWorkflowError(
            "narrative_writer_fact_binding_claim_handle_unknown"
        )
    resolved = next(
        (
            (material.material_handle, fact)
            for material in material_projection.evidence_materials
            for fact in material.facts
            if fact.fact_handle == fact_handle
        ),
        None,
    )
    if resolved is None:
        raise NarrativeWorkflowError(
            "narrative_writer_fact_binding_fact_handle_unknown"
        )
    material_handle, fact = resolved
    if material_handle not in set(claim.material_handles):
        raise NarrativeWorkflowError(
            "narrative_writer_fact_binding_claim_material_mismatch"
        )
    return NarrativeFactBinding.create(
        claim_handle=claim_handle,
        fact_handle=fact_handle,
        fact_kind=fact.fact_kind,
        value=fact.value,
        range_end=fact.range_end,
        unit=fact.unit,
    )


def _block_from_output(
    payload: Mapping[str, Any],
    *,
    writer_attempt_id: str,
    material_projection: NarrativeMaterialProjection,
) -> NarrativeBlock:
    return NarrativeBlock.create(
        writer_attempt_id=writer_attempt_id,
        role=payload["role"],
        text=payload["text"],
        claim_handles=payload["claim_handles"],
        recommendation_handles=payload["recommendation_handles"],
        limitation_handles=payload["limitation_handles"],
        material_fact_bindings=tuple(
            _fact_binding_from_output(
                item,
                material_projection=material_projection,
            )
            for item in payload["material_fact_bindings"]
        ),
        statement_role=payload["statement_role"],
        required=payload["required"],
    )


def _block_to_provider_payload(block: NarrativeBlock) -> dict[str, Any]:
    return {
        "role": block.role,
        "text": block.text,
        "claim_handles": list(block.claim_handles),
        "recommendation_handles": list(block.recommendation_handles),
        "limitation_handles": list(block.limitation_handles),
        "material_fact_bindings": [
            {
                "claim_handle": item.claim_handle,
                "fact_handle": item.fact_handle,
            }
            for item in block.material_fact_bindings
        ],
        "statement_role": block.statement_role,
        "required": block.required,
    }


def _block_to_focused_editable_payload(block: NarrativeBlock) -> dict[str, Any]:
    payload = _block_to_provider_payload(block)
    return {key: payload[key] for key in _FOCUSED_WRITER_EDITABLE_BLOCK_FIELDS}


def _writer_attempt_from_invocation(
    *,
    authority_bundle: AuthorityBundle,
    material_projection: NarrativeMaterialProjection,
    invocation: _ProviderInvocation,
) -> NarrativeWriterAttempt:
    response = invocation.responses[-1]
    return NarrativeWriterAttempt.create(
        authority_bundle_ref=authority_bundle.bundle_ref,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        input_ref=invocation.call_input.call_input_ref,
        input_digest=invocation.call_input.content_digest,
        attempt_number=response.attempt_number,
        provider_response=response,
    )


def _initial_writer_attempt_and_document(
    *,
    authority_bundle: AuthorityBundle,
    material_projection: NarrativeMaterialProjection,
    invocation: _ProviderInvocation,
) -> tuple[NarrativeWriterAttempt, NarrativeDocument]:
    attempt = _writer_attempt_from_invocation(
        authority_bundle=authority_bundle,
        material_projection=material_projection,
        invocation=invocation,
    )
    blocks = tuple(
        _block_from_output(
            item,
            writer_attempt_id=attempt.attempt_id,
            material_projection=material_projection,
        )
        for item in invocation.output["blocks"]
    )
    narrative = NarrativeDocument.create(
        authority_bundle_ref=authority_bundle.bundle_ref,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        writer_attempt=attempt,
        parent_narrative_id=None,
        blocks=blocks,
    )
    return attempt, narrative


def _focused_narrative_from_target_output(
    *,
    authority_bundle: AuthorityBundle,
    material_projection: NarrativeMaterialProjection,
    source_narrative: NarrativeDocument,
    writer_attempt: NarrativeWriterAttempt,
    target_output: Mapping[str, Any],
    retry_plan: _FocusedRetryPlan,
) -> NarrativeDocument:
    output_blocks = _mapping_sequence(
        target_output["blocks"],
        "focused_writer_blocks_invalid",
    )
    if len(output_blocks) != len(retry_plan.targets):
        raise NarrativeWorkflowError("focused_writer_target_count_invalid")
    materialized_targets = tuple(
        _block_from_output(
            _focused_output_block(
                output_block,
                target=target,
                material_projection=material_projection,
            ),
            writer_attempt_id=writer_attempt.attempt_id,
            material_projection=material_projection,
        )
        for output_block, target in zip(output_blocks, retry_plan.targets, strict=True)
    )
    replacement_by_source_id = {
        target.source_block.block_id: block
        for target, block in zip(retry_plan.targets, materialized_targets, strict=True)
        if target.source_block is not None
    }
    preserved_by_id = {item.block_id: item for item in retry_plan.preserved_blocks}
    merged_blocks = [
        (
            replacement_by_source_id[item.block_id]
            if item.block_id in replacement_by_source_id
            else preserved_by_id[item.block_id]
        )
        for item in source_narrative.blocks
        if item.block_id in replacement_by_source_id or item.block_id in preserved_by_id
    ]
    merged_blocks.extend(
        block
        for target, block in zip(retry_plan.targets, materialized_targets, strict=True)
        if target.target_kind == "insert"
    )
    return NarrativeDocument.create(
        authority_bundle_ref=authority_bundle.bundle_ref,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        writer_attempt=writer_attempt,
        parent_narrative_id=source_narrative.narrative_id,
        blocks=tuple(merged_blocks),
    )


def _sensitive_findings(
    inspector: SensitiveOutputInspector,
    *,
    narrative: NarrativeDocument,
    visibility_policy: PublicationFieldVisibilityPolicy,
) -> tuple[SensitiveOutputFinding, ...]:
    raw_findings = inspector(
        narrative=narrative,
        visibility_policy=visibility_policy,
    )
    if isinstance(raw_findings, (str, bytes)) or not isinstance(raw_findings, Sequence):
        raise NarrativeWorkflowError("sensitive_output_inspector_result_invalid")
    findings = tuple(raw_findings)
    if any(type(item) is not SensitiveOutputFinding for item in findings):
        raise NarrativeWorkflowError("sensitive_output_inspector_result_invalid")
    return findings


def _verifier_block_payload(block: NarrativeBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "content_digest": block.content_digest,
        **_block_to_provider_payload(block),
    }


def _requirement_limitation_scope(
    *,
    material_projection: NarrativeMaterialProjection,
    blocks: Sequence[NarrativeBlock],
) -> list[dict[str, Any]]:
    block_versions = tuple(blocks)
    limitations_by_handle = {
        item.limitation_handle: item for item in material_projection.limitations
    }
    claims_by_handle = {item.claim_handle: item for item in material_projection.claims}
    facets_by_handle = {
        item.boundary_facet_handle: item for item in material_projection.boundary_facets
    }
    scope: list[dict[str, Any]] = []
    for requirement in material_projection.publication_requirements:
        claim_options = tuple(requirement.claim_handles)
        required_limitations = tuple(requirement.limitation_handles)
        try:
            required_limitation_scope = []
            for handle in required_limitations:
                limitation = limitations_by_handle[handle]
                claim_binding_options = [
                    claim_handle
                    for claim_handle in claim_options
                    if handle in claims_by_handle[claim_handle].limitation_handles
                ]
                required_limitation_scope.append(
                    {
                        "limitation_handle": handle,
                        "binding_mode": (
                            "claim_or_boundary"
                            if claim_binding_options
                            else "boundary_only"
                        ),
                        "claim_binding_options": claim_binding_options,
                        "boundary_facet_handles": list(
                            limitation.boundary_facet_handles
                        ),
                        "boundary_facets": [
                            facets_by_handle[facet_handle].to_writer_payload()
                            for facet_handle in limitation.boundary_facet_handles
                        ],
                    }
                )
        except KeyError as exc:
            raise NarrativeWorkflowError(
                "requirement_limitation_scope_closure_invalid"
            ) from exc
        block_coverage = []
        for block in block_versions:
            covered_claims = tuple(
                handle for handle in claim_options if handle in block.claim_handles
            )
            bound_limitations = tuple(
                handle
                for handle in required_limitations
                if handle in block.limitation_handles
            )
            if not covered_claims and not bound_limitations:
                continue
            block_coverage.append(
                {
                    "block_id": block.block_id,
                    "content_digest": block.content_digest,
                    "covered_claim_handles": list(covered_claims),
                    "bound_required_limitation_handles": list(bound_limitations),
                    "missing_required_limitation_handles": [
                        handle
                        for handle in required_limitations
                        if handle not in block.limitation_handles
                    ],
                }
            )
        scope.append(
            {
                "requirement_handle": requirement.requirement_handle,
                "status": requirement.status,
                "coverage_semantics": requirement.coverage_semantics,
                "claim_kind": requirement.claim_kind,
                "assertion_scope": canonical_value(requirement.assertion_scope),
                "required_claim_strength": requirement.required_claim_strength,
                "claim_handle_options": list(claim_options),
                "required_limitations": required_limitation_scope,
                "block_coverage": block_coverage,
            }
        )
    return scope


def _focused_context_block_payload(
    *,
    block: NarrativeBlock,
    source_verifier_report: BlockVerifierReport,
    material_projection: NarrativeMaterialProjection,
) -> dict[str, Any]:
    return {
        **_verifier_block_payload(block),
        "settled_acceptance": {
            "source_verifier_report_ref": (source_verifier_report.verifier_report_ref),
            "source_verifier_report_digest": source_verifier_report.content_digest,
            "block_content_digest": block.content_digest,
            "material_projection_digest": material_projection.content_digest,
            "verifier_prompt_version": _NARRATIVE_PROMPT_VERSION,
        },
    }


def _verifier_payload(
    *,
    material_projection: NarrativeMaterialProjection,
    answer_context: NarrativeAnswerContext,
    narrative: NarrativeDocument,
    local_report: BlockLocalValidationReport,
    source_narrative: NarrativeDocument | None = None,
    source_local_report: BlockLocalValidationReport | None = None,
    source_verifier_report: BlockVerifierReport | None = None,
    source_verifier_call_input: NarrativeProviderCallInput | None = None,
) -> tuple[dict[str, Any], tuple[NarrativeBlock, ...], tuple[NarrativeBlock, ...]]:
    blocks_by_id = {item.block_id: item for item in narrative.blocks}
    try:
        accepted_locally = tuple(
            blocks_by_id[item] for item in local_report.accepted_block_ids
        )
    except KeyError as exc:
        raise NarrativeWorkflowError("block_verifier_local_scope_invalid") from exc
    source_values = (
        source_narrative,
        source_local_report,
        source_verifier_report,
        source_verifier_call_input,
    )
    if all(item is None for item in source_values):
        target_blocks = accepted_locally
        context_blocks: tuple[NarrativeBlock, ...] = ()
        scope = {
            "mode": "full",
            "verifier_prompt_version": _NARRATIVE_PROMPT_VERSION,
            "material_projection_ref": material_projection.projection_ref,
            "material_projection_digest": material_projection.content_digest,
            "source_verifier_report_ref": None,
            "source_verifier_report_digest": None,
            "source_verifier_input_ref": None,
            "source_verifier_input_digest": None,
            "inherited_acceptances": [],
            "target_block_ids": [item.block_id for item in target_blocks],
        }
        context_payload: list[dict[str, Any]] = []
    else:
        if any(item is None for item in source_values):
            raise NarrativeWorkflowError("block_verifier_source_scope_invalid")
        if (
            type(source_narrative) is not NarrativeDocument
            or type(source_local_report) is not BlockLocalValidationReport
            or type(source_verifier_report) is not BlockVerifierReport
            or type(source_verifier_call_input) is not NarrativeProviderCallInput
        ):
            raise NarrativeWorkflowError("block_verifier_source_scope_invalid")
        if (
            source_narrative.material_projection_ref
            != material_projection.projection_ref
            or source_narrative.material_projection_digest
            != material_projection.content_digest
            or narrative.material_projection_ref != material_projection.projection_ref
            or narrative.material_projection_digest
            != material_projection.content_digest
            or source_local_report.narrative_id != source_narrative.narrative_id
            or source_local_report.narrative_digest != source_narrative.content_digest
            or source_verifier_report.narrative_id != source_narrative.narrative_id
            or source_verifier_report.narrative_digest
            != source_narrative.content_digest
            or source_verifier_report.local_report_ref
            != source_local_report.local_report_ref
            or source_verifier_report.local_report_digest
            != source_local_report.content_digest
            or source_verifier_report.verifier_input_ref
            != source_verifier_call_input.call_input_ref
            or source_verifier_report.verifier_input_digest
            != source_verifier_call_input.content_digest
            or source_verifier_call_input.purpose != "block_verification"
            or source_verifier_call_input.material_projection_ref
            != material_projection.projection_ref
            or source_verifier_call_input.material_projection_digest
            != material_projection.content_digest
        ):
            raise NarrativeWorkflowError("block_verifier_source_scope_invalid")
        expected_source_payload, _, _ = _verifier_payload(
            material_projection=material_projection,
            answer_context=answer_context,
            narrative=source_narrative,
            local_report=source_local_report,
        )
        if canonical_value(source_verifier_call_input.payload) != canonical_value(
            expected_source_payload
        ):
            raise NarrativeWorkflowError("block_verifier_source_scope_invalid")
        source_blocks_by_id = {item.block_id: item for item in source_narrative.blocks}
        accepted_source_ids = frozenset(source_verifier_report.accepted_block_ids)
        context = []
        for block in accepted_locally:
            source_block = source_blocks_by_id.get(block.block_id)
            if source_block is None or block.block_id not in accepted_source_ids:
                continue
            if (
                source_block.content_digest != block.content_digest
                or source_block.to_dict() != block.to_dict()
            ):
                raise NarrativeWorkflowError(
                    "block_verifier_inherited_acceptance_invalid"
                )
            context.append(block)
        context_blocks = tuple(context)
        context_ids = {item.block_id for item in context_blocks}
        target_blocks = tuple(
            item for item in accepted_locally if item.block_id not in context_ids
        )
        inherited_acceptances = [
            {
                "block_id": item.block_id,
                "content_digest": item.content_digest,
                "source_verifier_report_ref": (
                    source_verifier_report.verifier_report_ref
                ),
                "source_verifier_report_digest": (
                    source_verifier_report.content_digest
                ),
                "material_projection_digest": material_projection.content_digest,
                "verifier_prompt_version": _NARRATIVE_PROMPT_VERSION,
            }
            for item in context_blocks
        ]
        scope = {
            "mode": "focused_retry",
            "verifier_prompt_version": _NARRATIVE_PROMPT_VERSION,
            "material_projection_ref": material_projection.projection_ref,
            "material_projection_digest": material_projection.content_digest,
            "source_verifier_report_ref": (source_verifier_report.verifier_report_ref),
            "source_verifier_report_digest": source_verifier_report.content_digest,
            "source_verifier_input_ref": (source_verifier_call_input.call_input_ref),
            "source_verifier_input_digest": source_verifier_call_input.content_digest,
            "inherited_acceptances": inherited_acceptances,
            "target_block_ids": [item.block_id for item in target_blocks],
        }
        context_payload = [
            _focused_context_block_payload(
                block=item,
                source_verifier_report=source_verifier_report,
                material_projection=material_projection,
            )
            for item in context_blocks
        ]
    payload = {
        "material_projection": material_projection.to_writer_payload(),
        "answer_context": answer_context.to_writer_payload(),
        "verification_scope": scope,
        "requirement_limitation_scope": _requirement_limitation_scope(
            material_projection=material_projection,
            blocks=accepted_locally,
        ),
        "context_blocks": context_payload,
        "blocks": [_verifier_block_payload(item) for item in target_blocks],
    }
    return payload, target_blocks, context_blocks


def _verifier_validator(
    output: Mapping[str, Any],
    *,
    blocks: Sequence[NarrativeBlock],
) -> None:
    _strict_mapping(
        output,
        frozenset({"decisions"}),
        "block_verifier_output_shape_invalid",
    )
    decisions = _mapping_sequence(
        output["decisions"], "block_verifier_decisions_invalid"
    )
    blocks_by_id = {item.block_id: item for item in blocks}
    if {item.get("block_id") for item in decisions} != set(blocks_by_id) or len(
        decisions
    ) != len(blocks_by_id):
        raise NarrativeWorkflowError("block_verifier_decision_coverage_invalid")
    for decision in decisions:
        _strict_mapping(
            decision,
            _VERIFIER_DECISION_FIELDS,
            "block_verifier_decision_shape_invalid",
        )
        block = blocks_by_id.get(decision["block_id"])
        if block is None or decision["disposition"] not in {"accepted", "vetoed"}:
            raise NarrativeWorkflowError("block_verifier_decision_invalid")
        claims = _sorted_string_tuple(
            decision["affected_claim_handles"],
            "block_verifier_claim_handles_invalid",
        )
        recommendations = _sorted_string_tuple(
            decision["affected_recommendation_handles"],
            "block_verifier_recommendation_handles_invalid",
        )
        limitations = _sorted_string_tuple(
            decision["limitation_handles"],
            "block_verifier_limitation_handles_invalid",
        )
        if (
            not set(claims).issubset(set(block.claim_handles))
            or not set(recommendations).issubset(set(block.recommendation_handles))
            or not set(limitations).issubset(set(block.limitation_handles))
        ):
            raise NarrativeWorkflowError("block_verifier_handle_closure_invalid")
        if decision["disposition"] == "accepted":
            if (
                decision["reason_code"] is not None
                or claims
                or recommendations
                or limitations
            ):
                raise NarrativeWorkflowError("block_verifier_acceptance_invalid")
        elif (
            not isinstance(decision["reason_code"], str)
            or not decision["reason_code"].strip()
            or decision["reason_code"] != decision["reason_code"].strip()
            or not (claims or recommendations or limitations)
        ):
            raise NarrativeWorkflowError("block_verifier_veto_invalid")


def _verifier_report_from_output(
    *,
    material_projection: NarrativeMaterialProjection,
    visibility_policy: PublicationFieldVisibilityPolicy,
    narrative: NarrativeDocument,
    local_report: BlockLocalValidationReport,
    verification_attempt: BlockVerificationAttempt,
    output: Mapping[str, Any],
    target_blocks: Sequence[NarrativeBlock],
    inherited_blocks: Sequence[NarrativeBlock],
) -> BlockVerifierReport:
    target_versions = tuple(target_blocks)
    inherited_versions = tuple(inherited_blocks)
    _verifier_validator(output, blocks=target_versions)
    decisions = tuple(output["decisions"])
    accepted = tuple(item.block_id for item in inherited_versions) + tuple(
        item["block_id"] for item in decisions if item["disposition"] == "accepted"
    )
    vetoes = tuple(
        BlockVeto.create(
            narrative_id=narrative.narrative_id,
            block_id=item["block_id"],
            reason_code=item["reason_code"],
            affected_claim_handles=item["affected_claim_handles"],
            affected_recommendation_handles=item["affected_recommendation_handles"],
            limitation_handles=item["limitation_handles"],
        )
        for item in decisions
        if item["disposition"] == "vetoed"
    )
    return BlockVerifierReport.create(
        narrative=narrative,
        material_projection=material_projection,
        visibility_policy=visibility_policy,
        local_report=local_report,
        verification_attempt=verification_attempt,
        accepted_block_ids=accepted,
        vetoes=vetoes,
    )


def _verify_narrative(
    *,
    authority_bundle: AuthorityBundle,
    material_projection: NarrativeMaterialProjection,
    visibility_policy: PublicationFieldVisibilityPolicy,
    answer_context: NarrativeAnswerContext,
    narrative: NarrativeDocument,
    local_report: BlockLocalValidationReport,
    llm_client: TypedNarrativeLLM,
    source_narrative: NarrativeDocument | None = None,
    source_local_report: BlockLocalValidationReport | None = None,
    source_verifier_report: BlockVerifierReport | None = None,
    source_verifier_call_input: NarrativeProviderCallInput | None = None,
) -> tuple[_ProviderInvocation, BlockVerificationAttempt, BlockVerifierReport]:
    payload, target_blocks, inherited_blocks = _verifier_payload(
        material_projection=material_projection,
        answer_context=answer_context,
        narrative=narrative,
        local_report=local_report,
        source_narrative=source_narrative,
        source_local_report=source_local_report,
        source_verifier_report=source_verifier_report,
        source_verifier_call_input=source_verifier_call_input,
    )
    call_input = NarrativeProviderCallInput.create(
        purpose="block_verification",
        authority_bundle=authority_bundle,
        material_projection=material_projection,
        payload=payload,
    )

    def validator(output: Mapping[str, Any]) -> None:
        _verifier_validator(output, blocks=target_blocks)

    invocation = _invoke_provider(
        llm_client,
        call_input=call_input,
        system_prompt=_VERIFIER_SYSTEM_PROMPT,
        required_key="decisions",
        validator=validator,
    )
    response = invocation.responses[-1]
    attempt = BlockVerificationAttempt.create(
        narrative=narrative,
        local_report=local_report,
        input_ref=call_input.call_input_ref,
        input_digest=call_input.content_digest,
        attempt_number=response.attempt_number,
        provider_response=response,
    )
    report = _verifier_report_from_output(
        material_projection=material_projection,
        visibility_policy=visibility_policy,
        narrative=narrative,
        local_report=local_report,
        verification_attempt=attempt,
        output=invocation.output,
        target_blocks=target_blocks,
        inherited_blocks=inherited_blocks,
    )
    return invocation, attempt, report


def _focused_retry_required_coverage(
    *,
    preserved_blocks: Sequence[NarrativeBlock],
    material_projection: NarrativeMaterialProjection,
) -> tuple[dict[str, Any], ...]:
    accepted_required_blocks = tuple(
        block for block in preserved_blocks if block.required
    )
    covered_claim_handles = frozenset(
        handle for block in accepted_required_blocks for handle in block.claim_handles
    )
    covered_limitation_handles = frozenset(
        handle
        for block in accepted_required_blocks
        for handle in block.limitation_handles
    )
    required_coverage: list[dict[str, Any]] = []
    for requirement in material_projection.publication_requirements:
        if requirement.status in {"satisfied", "mixed", "contradicted"}:
            claim_handle_options = (
                ()
                if not covered_claim_handles.isdisjoint(requirement.claim_handles)
                else requirement.claim_handles
            )
        elif requirement.status == "unavailable":
            claim_handle_options = ()
        else:
            raise NarrativeWorkflowError(
                "narrative_publication_requirement_status_invalid"
            )
        required_limitation_handles = tuple(
            handle
            for handle in requirement.limitation_handles
            if handle not in covered_limitation_handles
        )
        if claim_handle_options or required_limitation_handles:
            required_coverage.append(
                {
                    "requirement_handle": requirement.requirement_handle,
                    "claim_handle_options": list(claim_handle_options),
                    "required_limitation_handles": list(required_limitation_handles),
                }
            )
    return tuple(required_coverage)


@dataclass(frozen=True)
class _FocusedTargetCoverage:
    requirement_handle: str
    claim_handle_options: tuple[str, ...]
    required_limitation_handles: tuple[str, ...]

    def to_provider_payload(self) -> dict[str, Any]:
        return {
            "requirement_handle": self.requirement_handle,
            "claim_handle_options": list(self.claim_handle_options),
            "required_limitation_handles": list(self.required_limitation_handles),
        }


@dataclass(frozen=True)
class _FocusedRetryTarget:
    target_id: str
    target_kind: str
    role: str
    required: bool
    source_block: NarrativeBlock | None
    required_coverage: tuple[_FocusedTargetCoverage, ...]
    allowed_claim_handles: tuple[str, ...]
    allowed_recommendation_handles: tuple[str, ...]
    allowed_limitation_handles: tuple[str, ...]
    source_seed_limitation_handles: tuple[str, ...]


@dataclass(frozen=True)
class _FocusedRetryPlan:
    targets: tuple[_FocusedRetryTarget, ...]
    preserved_blocks: tuple[NarrativeBlock, ...]
    open_required_coverage: tuple[dict[str, Any], ...]

    @property
    def source_target_blocks(self) -> tuple[NarrativeBlock, ...]:
        return tuple(
            target.source_block
            for target in self.targets
            if target.source_block is not None
        )


def _block_can_carry_limitation(
    block: NarrativeBlock,
    limitation_handle: str,
    *,
    material_projection: NarrativeMaterialProjection,
) -> bool:
    if block.role == "boundary":
        return True
    claims = {item.claim_handle: item for item in material_projection.claims}
    recommendations = {
        item.recommendation_handle: item for item in material_projection.recommendations
    }
    return any(
        limitation_handle in claims[handle].limitation_handles
        for handle in block.claim_handles
        if handle in claims
    ) or any(
        limitation_handle in recommendations[handle].risk_handles
        for handle in block.recommendation_handles
        if handle in recommendations
    )


def _focused_target_handle_space(
    *,
    role: str,
    source_block: NarrativeBlock | None,
    required_coverage: Sequence[_FocusedTargetCoverage],
    material_projection: NarrativeMaterialProjection,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    claim_by_handle = {item.claim_handle: item for item in material_projection.claims}
    recommendation_by_handle = {
        item.recommendation_handle: item for item in material_projection.recommendations
    }
    known_limitation_handles = {
        item.limitation_handle for item in material_projection.limitations
    }
    source_claim_handles = tuple(
        handle
        for handle in (() if source_block is None else source_block.claim_handles)
        if handle in claim_by_handle
    )
    source_recommendation_handles = tuple(
        handle
        for handle in (
            () if source_block is None else source_block.recommendation_handles
        )
        if handle in recommendation_by_handle
    )
    supporting_claim_handles = tuple(
        dict.fromkeys(
            claim_handle
            for recommendation_handle in source_recommendation_handles
            for claim_handle in recommendation_by_handle[
                recommendation_handle
            ].supporting_claim_handles
            if claim_handle in claim_by_handle
        )
    )
    allowed_claim_handles = tuple(
        dict.fromkeys(
            source_claim_handles
            + tuple(
                handle
                for coverage in required_coverage
                for handle in coverage.claim_handle_options
            )
            + supporting_claim_handles
        )
    )
    allowed_recommendation_handles = source_recommendation_handles
    scoped_limitation_handles = tuple(
        dict.fromkeys(
            handle
            for claim_handle in allowed_claim_handles
            for handle in claim_by_handle[claim_handle].limitation_handles
        )
    ) + tuple(
        dict.fromkeys(
            handle
            for recommendation_handle in allowed_recommendation_handles
            for handle in recommendation_by_handle[recommendation_handle].risk_handles
        )
    )
    source_scoped_limitation_handles = tuple(
        dict.fromkeys(
            handle
            for claim_handle in source_claim_handles
            for handle in claim_by_handle[claim_handle].limitation_handles
        )
    ) + tuple(
        dict.fromkeys(
            handle
            for recommendation_handle in source_recommendation_handles
            for handle in recommendation_by_handle[recommendation_handle].risk_handles
        )
    )
    required_limitation_handles = tuple(
        handle
        for coverage in required_coverage
        for handle in coverage.required_limitation_handles
    )
    source_limitation_handles = (
        ()
        if source_block is None
        else tuple(
            handle
            for handle in source_block.limitation_handles
            if handle in known_limitation_handles
        )
    )
    if role == "boundary":
        allowed_limitation_handles = tuple(
            dict.fromkeys(
                source_limitation_handles
                + required_limitation_handles
                + scoped_limitation_handles
            )
        )
        source_seed_limitation_handles = source_limitation_handles
    else:
        scoped = frozenset(scoped_limitation_handles)
        allowed_limitation_handles = tuple(
            handle
            for handle in dict.fromkeys(
                source_limitation_handles
                + required_limitation_handles
                + scoped_limitation_handles
            )
            if handle in scoped
        )
        source_scope = frozenset(source_scoped_limitation_handles)
        source_seed_limitation_handles = tuple(
            handle for handle in source_limitation_handles if handle in source_scope
        )
    return (
        allowed_claim_handles,
        allowed_recommendation_handles,
        allowed_limitation_handles,
        source_seed_limitation_handles,
    )


def _assert_focused_retry_plan_satisfiable(
    *,
    targets: Sequence[_FocusedRetryTarget],
    open_required_coverage: Sequence[Mapping[str, Any]],
    material_projection: NarrativeMaterialProjection,
) -> None:
    known_claim_handles = {item.claim_handle for item in material_projection.claims}
    known_recommendation_handles = {
        item.recommendation_handle for item in material_projection.recommendations
    }
    known_limitation_handles = {
        item.limitation_handle for item in material_projection.limitations
    }
    expected = {
        item["requirement_handle"]: (
            frozenset(item["claim_handle_options"]),
            frozenset(item["required_limitation_handles"]),
        )
        for item in open_required_coverage
    }
    assigned: dict[str, tuple[set[str], set[str]]] = {}
    for target in targets:
        if (
            not set(target.allowed_claim_handles).issubset(known_claim_handles)
            or not set(target.allowed_recommendation_handles).issubset(
                known_recommendation_handles
            )
            or not set(target.allowed_limitation_handles).issubset(
                known_limitation_handles
            )
            or not set(target.source_seed_limitation_handles).issubset(
                target.allowed_limitation_handles
            )
        ):
            raise NarrativeWorkflowError("focused_retry_plan_handle_space_invalid")
        if (
            (target.role == "boundary" and not target.allowed_limitation_handles)
            or (
                target.role == "next_action"
                and not target.allowed_recommendation_handles
            )
            or (
                target.role not in {"boundary", "next_action"}
                and not (
                    target.allowed_claim_handles
                    or target.allowed_recommendation_handles
                )
            )
        ):
            raise NarrativeWorkflowError("focused_retry_plan_target_authority_missing")
        for coverage in target.required_coverage:
            if (
                coverage.requirement_handle not in expected
                or not set(coverage.claim_handle_options).issubset(
                    target.allowed_claim_handles
                )
                or not set(coverage.required_limitation_handles).issubset(
                    target.allowed_limitation_handles
                )
            ):
                raise NarrativeWorkflowError("focused_retry_plan_handle_space_invalid")
            assigned_claims, assigned_limitations = assigned.setdefault(
                coverage.requirement_handle,
                (set(), set()),
            )
            coverage_claims = set(coverage.claim_handle_options)
            coverage_limitations = set(coverage.required_limitation_handles)
            if assigned_claims.intersection(
                coverage_claims
            ) or assigned_limitations.intersection(coverage_limitations):
                raise NarrativeWorkflowError(
                    "focused_retry_plan_coverage_assignment_invalid"
                )
            assigned_claims.update(coverage_claims)
            assigned_limitations.update(coverage_limitations)
    normalized_assigned = {
        requirement_handle: (
            frozenset(claim_handles),
            frozenset(limitation_handles),
        )
        for requirement_handle, (
            claim_handles,
            limitation_handles,
        ) in assigned.items()
    }
    if normalized_assigned != expected:
        raise NarrativeWorkflowError("focused_retry_plan_coverage_assignment_invalid")


def _compile_focused_retry_plan(
    *,
    source_order: Sequence[NarrativeBlock],
    accepted_block_ids: Sequence[str],
    rejected_block_ids: Sequence[str],
    material_projection: NarrativeMaterialProjection,
) -> _FocusedRetryPlan:
    source = tuple(source_order)
    source_ids = tuple(item.block_id for item in source)
    accepted = frozenset(accepted_block_ids)
    rejected = frozenset(rejected_block_ids)
    if (
        not source
        or len(source_ids) != len(set(source_ids))
        or accepted.intersection(rejected)
        or accepted.union(rejected) != set(source_ids)
    ):
        raise NarrativeWorkflowError("focused_retry_plan_source_closure_invalid")
    target_ids = {
        item.block_id for item in source if item.required and item.block_id in rejected
    }
    if not target_ids:
        raise NarrativeWorkflowError("focused_retry_plan_target_missing")
    preserved_ids = set(accepted)
    provisional_targets = tuple(item for item in source if item.block_id in target_ids)
    provisional_preserved = tuple(
        item for item in source if item.block_id in preserved_ids
    )
    provisional_coverage = _focused_retry_required_coverage(
        preserved_blocks=provisional_preserved,
        material_projection=material_projection,
    )
    open_limitations = tuple(
        handle
        for requirement in provisional_coverage
        for handle in requirement["required_limitation_handles"]
    )
    boundary_required = tuple(
        handle
        for handle in dict.fromkeys(open_limitations)
        if not any(
            _block_can_carry_limitation(
                block,
                handle,
                material_projection=material_projection,
            )
            for block in provisional_targets
        )
    )
    boundary_source = next(
        (
            block
            for block in provisional_targets
            if block.required and block.role == "boundary"
        ),
        None,
    )
    if boundary_required and boundary_source is None:
        boundary_source = next(
            (
                block
                for block in provisional_preserved
                if block.required and block.role == "boundary"
            ),
            None,
        )
        if boundary_source is not None:
            target_ids.add(boundary_source.block_id)
            preserved_ids.remove(boundary_source.block_id)

    source_targets = tuple(item for item in source if item.block_id in target_ids)
    preserved_blocks = tuple(item for item in source if item.block_id in preserved_ids)
    open_coverage = _focused_retry_required_coverage(
        preserved_blocks=preserved_blocks,
        material_projection=material_projection,
    )
    open_limitations = tuple(
        handle
        for requirement in open_coverage
        for handle in requirement["required_limitation_handles"]
    )
    boundary_required = tuple(
        handle
        for handle in dict.fromkeys(open_limitations)
        if not any(
            _block_can_carry_limitation(
                block,
                handle,
                material_projection=material_projection,
            )
            for block in source_targets
        )
    )
    target_seeds: list[tuple[str, str, bool, NarrativeBlock | None]] = [
        (block.block_id, block.role, block.required, block) for block in source_targets
    ]
    if boundary_required and not any(
        role == "boundary" and required for _, role, required, _ in target_seeds
    ):
        slot_digest = canonical_digest(
            {
                "source_block_ids": source_ids,
                "accepted_block_ids": tuple(sorted(accepted)),
                "rejected_block_ids": tuple(sorted(rejected)),
                "required_limitation_handles": boundary_required,
            }
        )
        target_seeds.append(
            (
                "focused-boundary-slot:sha256:" + slot_digest,
                "boundary",
                True,
                None,
            )
        )

    assignments: dict[str, dict[str, dict[str, list[str]]]] = {
        target_id: {} for target_id, _, _, _ in target_seeds
    }

    def assignment_for(
        target_id: str,
        requirement_handle: str,
    ) -> dict[str, list[str]]:
        return assignments[target_id].setdefault(
            requirement_handle,
            {"claims": [], "limitations": []},
        )

    for requirement in open_coverage:
        requirement_handle = requirement["requirement_handle"]
        claim_options = tuple(requirement["claim_handle_options"])
        if claim_options:
            claim_target = next(
                (
                    seed
                    for seed in target_seeds
                    if seed[3] is not None
                    and not set(seed[3].claim_handles).isdisjoint(claim_options)
                ),
                None,
            )
            if claim_target is None:
                raise NarrativeWorkflowError("focused_retry_plan_claim_target_missing")
            assignment_for(claim_target[0], requirement_handle)["claims"].extend(
                claim_options
            )
        for limitation_handle in requirement["required_limitation_handles"]:
            limitation_target = next(
                (
                    seed
                    for seed in target_seeds
                    if seed[3] is not None
                    and limitation_handle in seed[3].limitation_handles
                    and _block_can_carry_limitation(
                        seed[3],
                        limitation_handle,
                        material_projection=material_projection,
                    )
                ),
                None,
            )
            if limitation_target is None:
                limitation_target = next(
                    (
                        seed
                        for seed in target_seeds
                        if seed[1] != "boundary"
                        and seed[3] is not None
                        and _block_can_carry_limitation(
                            seed[3],
                            limitation_handle,
                            material_projection=material_projection,
                        )
                    ),
                    None,
                )
            if limitation_target is None:
                limitation_target = next(
                    (
                        seed
                        for seed in target_seeds
                        if seed[1] == "boundary" and seed[2]
                    ),
                    None,
                )
            if limitation_target is None:
                raise NarrativeWorkflowError(
                    "focused_retry_plan_limitation_target_missing"
                )
            assignment_for(limitation_target[0], requirement_handle)[
                "limitations"
            ].append(limitation_handle)

    targets: list[_FocusedRetryTarget] = []
    for target_id, role, required, source_block in target_seeds:
        required_coverage = tuple(
            _FocusedTargetCoverage(
                requirement_handle=requirement_handle,
                claim_handle_options=tuple(dict.fromkeys(values["claims"])),
                required_limitation_handles=tuple(dict.fromkeys(values["limitations"])),
            )
            for requirement_handle, values in assignments[target_id].items()
        )
        (
            allowed_claim_handles,
            allowed_recommendation_handles,
            allowed_limitation_handles,
            source_seed_limitation_handles,
        ) = _focused_target_handle_space(
            role=role,
            source_block=source_block,
            required_coverage=required_coverage,
            material_projection=material_projection,
        )
        targets.append(
            _FocusedRetryTarget(
                target_id=target_id,
                target_kind=("replace" if source_block is not None else "insert"),
                role=role,
                required=required,
                source_block=source_block,
                required_coverage=required_coverage,
                allowed_claim_handles=allowed_claim_handles,
                allowed_recommendation_handles=(allowed_recommendation_handles),
                allowed_limitation_handles=allowed_limitation_handles,
                source_seed_limitation_handles=(source_seed_limitation_handles),
            )
        )
    _assert_focused_retry_plan_satisfiable(
        targets=targets,
        open_required_coverage=open_coverage,
        material_projection=material_projection,
    )
    return _FocusedRetryPlan(
        targets=tuple(targets),
        preserved_blocks=preserved_blocks,
        open_required_coverage=open_coverage,
    )


def _focused_scoped_material_view(
    *,
    retry_plan: _FocusedRetryPlan,
    material_projection: NarrativeMaterialProjection,
) -> dict[str, Any]:
    full = material_projection.to_writer_payload()
    relevant_claim_handles = {
        handle
        for target in retry_plan.targets
        for handle in target.allowed_claim_handles
    }
    relevant_recommendation_handles = {
        handle
        for target in retry_plan.targets
        for handle in target.allowed_recommendation_handles
    }
    recommendation_by_handle = {
        item["recommendation_handle"]: item for item in full["recommendations"]
    }
    for handle in tuple(relevant_recommendation_handles):
        recommendation = recommendation_by_handle.get(handle)
        if recommendation is not None:
            relevant_claim_handles.update(recommendation["supporting_claim_handles"])
    claims = [
        item
        for item in full["claims"]
        if item["claim_handle"] in relevant_claim_handles
    ]
    relevant_material_handles = {
        handle for claim in claims for handle in claim["material_handles"]
    }
    evidence_materials = [
        item
        for item in full["evidence_materials"]
        if item["material_handle"] in relevant_material_handles
    ]
    facts_by_material = {
        item["material_handle"]: tuple(fact["fact_handle"] for fact in item["facts"])
        for item in evidence_materials
    }
    allowed_claim_fact_pairs = [
        {
            "claim_handle": claim["claim_handle"],
            "fact_handles": list(
                dict.fromkeys(
                    fact_handle
                    for material_handle in claim["material_handles"]
                    for fact_handle in facts_by_material.get(material_handle, ())
                )
            ),
        }
        for claim in claims
    ]
    relevant_limitation_handles = {
        handle
        for target in retry_plan.targets
        for handle in target.allowed_limitation_handles
    }
    recommendations = [
        item
        for item in full["recommendations"]
        if item["recommendation_handle"] in relevant_recommendation_handles
    ]
    limitations = [
        item
        for item in full["limitations"]
        if item["limitation_handle"] in relevant_limitation_handles
    ]
    relevant_facet_handles = {
        handle
        for limitation in limitations
        for handle in limitation["boundary_facet_handles"]
    }
    open_requirement_handles = {
        item["requirement_handle"] for item in retry_plan.open_required_coverage
    }
    limitation_scope = [
        {
            "limitation_handle": limitation["limitation_handle"],
            "claim_handles": [
                claim["claim_handle"]
                for claim in claims
                if limitation["limitation_handle"] in claim["limitation_handles"]
            ],
            "recommendation_handles": [
                recommendation["recommendation_handle"]
                for recommendation in recommendations
                if limitation["limitation_handle"] in recommendation["risk_handles"]
            ],
            "boundary_allowed": True,
        }
        for limitation in limitations
    ]
    return {
        "authority_mode": full["authority_mode"],
        "claims": claims,
        "publication_requirements": [
            item
            for item in full["publication_requirements"]
            if item["requirement_handle"] in open_requirement_handles
        ],
        "evidence_materials": evidence_materials,
        "recommendations": recommendations,
        "limitations": limitations,
        "boundary_facets": [
            item
            for item in full["boundary_facets"]
            if item["boundary_facet_handle"] in relevant_facet_handles
        ],
        "allowed_claim_fact_pairs": allowed_claim_fact_pairs,
        "limitation_scope": limitation_scope,
    }


def _focused_requirement_limitation_scope(
    *,
    retry_plan: _FocusedRetryPlan,
    material_projection: NarrativeMaterialProjection,
) -> list[dict[str, Any]]:
    return _requirement_limitation_scope(
        material_projection=material_projection,
        blocks=(*retry_plan.preserved_blocks, *retry_plan.source_target_blocks),
    )


def _focused_editable_source_payload(
    target: _FocusedRetryTarget,
) -> dict[str, Any] | None:
    source_block = target.source_block
    editable_source_block = (
        None
        if source_block is None
        else _block_to_focused_editable_payload(source_block)
    )
    if editable_source_block is not None:
        editable_source_block["claim_handles"] = [
            handle
            for handle in editable_source_block["claim_handles"]
            if handle in target.allowed_claim_handles
        ]
        editable_source_block["recommendation_handles"] = [
            handle
            for handle in editable_source_block["recommendation_handles"]
            if handle in target.allowed_recommendation_handles
        ]
        editable_source_block["limitation_handles"] = [
            handle
            for handle in editable_source_block["limitation_handles"]
            if handle in target.source_seed_limitation_handles
        ]
        editable_source_block["material_fact_bindings"] = [
            item
            for item in editable_source_block["material_fact_bindings"]
            if item["claim_handle"] in target.allowed_claim_handles
        ]
    return editable_source_block


def _focused_target_to_provider_payload(
    target: _FocusedRetryTarget,
    *,
    local_report: BlockLocalValidationReport,
    verifier_report: BlockVerifierReport,
) -> dict[str, Any]:
    source_block = target.source_block
    return {
        "target_id": target.target_id,
        "target_kind": target.target_kind,
        "fixed_role": target.role,
        "fixed_required": target.required,
        "editable_source_block": _focused_editable_source_payload(target),
        "allowed_claim_handles": list(target.allowed_claim_handles),
        "allowed_recommendation_handles": list(target.allowed_recommendation_handles),
        "allowed_limitation_handles": list(target.allowed_limitation_handles),
        "local_issue_codes": (
            []
            if source_block is None
            else sorted(
                item.code
                for item in local_report.issues
                if item.block_id == source_block.block_id
            )
        ),
        "semantic_veto_reason_codes": (
            []
            if source_block is None
            else sorted(
                item.reason_code
                for item in verifier_report.vetoes
                if item.block_id == source_block.block_id
            )
        ),
        "required_coverage": [
            item.to_provider_payload() for item in target.required_coverage
        ],
    }


def _focused_retry_answer_context_payload(
    *,
    answer_context: NarrativeAnswerContext,
    source_narrative: NarrativeDocument,
    rejection_report: BlockVerifierReport,
    local_report: BlockLocalValidationReport,
    retry_plan: _FocusedRetryPlan,
) -> dict[str, Any]:
    return {
        **answer_context.to_writer_payload(),
        "focused_retry": {
            "source_narrative_id": source_narrative.narrative_id,
            "rejection_report_ref": rejection_report.verifier_report_ref,
            "accepted_sibling_blocks": [
                _block_to_provider_payload(item) for item in retry_plan.preserved_blocks
            ],
            "retry_targets": [
                _focused_target_to_provider_payload(
                    item,
                    local_report=local_report,
                    verifier_report=rejection_report,
                )
                for item in retry_plan.targets
            ],
            "required_coverage": list(retry_plan.open_required_coverage),
        },
    }


def _focused_output_block(
    payload: Mapping[str, Any],
    *,
    target: _FocusedRetryTarget,
    material_projection: NarrativeMaterialProjection,
) -> dict[str, Any]:
    _strict_mapping(
        payload,
        _FOCUSED_WRITER_EDITABLE_BLOCK_FIELDS,
        "focused_writer_block_shape_invalid",
    )
    block = {
        "role": target.role,
        **dict(payload),
        "required": target.required,
    }
    _writer_block_shape(block, material_projection=material_projection)
    claims = {item.claim_handle: item for item in material_projection.claims}
    recommendations = {
        item.recommendation_handle: item for item in material_projection.recommendations
    }
    limitations = {item.limitation_handle for item in material_projection.limitations}
    if any(handle not in claims for handle in block["claim_handles"]) or any(
        handle not in recommendations for handle in block["recommendation_handles"]
    ):
        raise NarrativeWorkflowError("focused_writer_authority_handle_unknown")
    claim_handles = set(block["claim_handles"])
    limitation_handles = set(block["limitation_handles"])
    for coverage in target.required_coverage:
        if coverage.claim_handle_options and claim_handles.isdisjoint(
            coverage.claim_handle_options
        ):
            raise NarrativeWorkflowError("focused_writer_target_claim_coverage_invalid")
        if not set(coverage.required_limitation_handles).issubset(limitation_handles):
            raise NarrativeWorkflowError(
                "focused_writer_target_limitation_coverage_invalid"
            )
    if (
        not claim_handles.issubset(target.allowed_claim_handles)
        or not set(block["recommendation_handles"]).issubset(
            target.allowed_recommendation_handles
        )
        or not limitation_handles.issubset(target.allowed_limitation_handles)
    ):
        raise NarrativeWorkflowError("focused_writer_target_handle_scope_invalid")
    allowed_limitations = (
        limitations
        if target.role == "boundary"
        else {
            handle
            for claim_handle in block["claim_handles"]
            for handle in claims[claim_handle].limitation_handles
        }.union(
            handle
            for recommendation_handle in block["recommendation_handles"]
            for handle in recommendations[recommendation_handle].risk_handles
        )
    )
    if not limitation_handles.issubset(allowed_limitations):
        raise NarrativeWorkflowError("focused_writer_limitation_scope_invalid")
    return block


def _validated_focused_writer_merge(
    output: Mapping[str, Any],
    *,
    source_order: Sequence[NarrativeBlock],
    retry_plan: _FocusedRetryPlan,
    authority_mode: str,
    material_projection: NarrativeMaterialProjection,
) -> dict[str, Any]:
    _strict_mapping(
        output,
        frozenset({"blocks"}),
        "focused_writer_output_shape_invalid",
    )
    output_blocks = _mapping_sequence(
        output["blocks"],
        "focused_writer_blocks_invalid",
    )
    source = tuple(source_order)
    source_ids = {item.block_id for item in source}
    source_targets = retry_plan.source_target_blocks
    source_target_ids = {item.block_id for item in source_targets}
    preserved_ids = {item.block_id for item in retry_plan.preserved_blocks}
    if source_target_ids.intersection(preserved_ids) or not source_target_ids.union(
        preserved_ids
    ).issubset(source_ids):
        raise NarrativeWorkflowError("focused_writer_source_closure_invalid")
    if len(output_blocks) != len(retry_plan.targets):
        raise NarrativeWorkflowError("focused_writer_target_count_invalid")
    resolved_blocks = tuple(
        _focused_output_block(
            output_block,
            target=target,
            material_projection=material_projection,
        )
        for output_block, target in zip(output_blocks, retry_plan.targets, strict=True)
    )
    output_by_target_id = {
        target.target_id: block
        for target, block in zip(retry_plan.targets, resolved_blocks, strict=True)
    }
    preserved_by_id = {item.block_id: item for item in retry_plan.preserved_blocks}
    merged_blocks = [
        (
            output_by_target_id[item.block_id]
            if item.block_id in output_by_target_id
            else _block_to_provider_payload(preserved_by_id[item.block_id])
        )
        for item in source
        if item.block_id in source_target_ids or item.block_id in preserved_by_id
    ]
    merged_blocks.extend(
        output_by_target_id[target.target_id]
        for target in retry_plan.targets
        if target.target_kind == "insert"
    )
    merged_output = {"blocks": merged_blocks}
    _initial_writer_validator(
        merged_output,
        authority_mode=authority_mode,
        material_projection=material_projection,
    )
    return merged_output


def _focused_writer_validator(
    output: Mapping[str, Any],
    *,
    source_order: Sequence[NarrativeBlock],
    retry_plan: _FocusedRetryPlan,
    authority_mode: str,
    material_projection: NarrativeMaterialProjection,
) -> None:
    _validated_focused_writer_merge(
        output,
        source_order=source_order,
        retry_plan=retry_plan,
        authority_mode=authority_mode,
        material_projection=material_projection,
    )


def _workflow_result(
    *,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    public_materialization: ReviewedPublicFactMaterialization,
    visibility_policy: PublicationFieldVisibilityPolicy,
    answer_context: NarrativeAnswerContext,
    material_projection: NarrativeMaterialProjection,
    provider_call_inputs: Sequence[NarrativeProviderCallInput],
    provider_responses: Sequence[RestrictedProviderResponse],
    provider_audits: Sequence[NarrativeProviderCallAudit],
    writer_attempts: Sequence[NarrativeWriterAttempt],
    narratives: Sequence[NarrativeDocument],
    local_reports: Sequence[BlockLocalValidationReport],
    verification_attempts: Sequence[BlockVerificationAttempt],
    verifier_reports: Sequence[BlockVerifierReport],
    focused_retry: FocusedNarrativeRetry | None,
) -> NarrativeWorkflowResult:
    settlement = _validated_settlement(claim_settlement)
    _assert_bundle_settlement_closure(authority_bundle, settlement)
    if (
        type(public_materialization) is not ReviewedPublicFactMaterialization
        or public_materialization.authority_bundle_ref != authority_bundle.bundle_ref
        or public_materialization.authority_bundle_digest
        != authority_bundle.bundle_digest
        or public_materialization.claim_settlement_ref != settlement.settlement_ref
        or public_materialization.claim_settlement_digest != settlement.content_digest
    ):
        raise NarrativeWorkflowError("narrative_workflow_materialization_invalid")
    if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
        raise NarrativeWorkflowError("narrative_workflow_visibility_policy_invalid")
    if (
        PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
        != visibility_policy
    ):
        raise NarrativeWorkflowError("narrative_workflow_visibility_policy_invalid")
    if type(answer_context) is not NarrativeAnswerContext or (
        NarrativeAnswerContext.from_dict(answer_context.to_dict()) != answer_context
    ):
        raise NarrativeWorkflowError("narrative_workflow_answer_context_invalid")
    if type(material_projection) is not NarrativeMaterialProjection:
        raise NarrativeWorkflowError("narrative_workflow_material_projection_invalid")
    material_projection.assert_integrity()
    if material_projection.authority_mode != authority_bundle.authority_mode:
        raise NarrativeWorkflowError(
            "narrative_workflow_material_projection_closure_invalid"
        )
    call_inputs = tuple(provider_call_inputs)
    responses = tuple(provider_responses)
    audits = tuple(provider_audits)
    writers = tuple(writer_attempts)
    narrative_versions = tuple(narratives)
    local_versions = tuple(local_reports)
    verifier_attempt_versions = tuple(verification_attempts)
    verifier_report_versions = tuple(verifier_reports)
    version_count = len(narrative_versions)
    if (
        version_count not in {1, 2}
        or len(writers) != version_count
        or len(local_versions) != version_count
        or len(verifier_attempt_versions) != version_count
        or len(verifier_report_versions) != version_count
        or len(call_inputs) != version_count * 2
        or len(audits) != len(call_inputs)
    ):
        raise NarrativeWorkflowError("narrative_workflow_artifact_cardinality_invalid")
    expected_purposes = tuple(
        purpose
        for _ in range(version_count)
        for purpose in ("narrative_writer", "block_verification")
    )
    if (
        tuple(item.purpose for item in call_inputs) != expected_purposes
        or tuple(item.purpose for item in audits) != expected_purposes
    ):
        raise NarrativeWorkflowError("narrative_workflow_call_order_invalid")
    if any(
        audit.call_input_ref != call_input.call_input_ref
        for call_input, audit in zip(call_inputs, audits, strict=True)
    ):
        raise NarrativeWorkflowError("narrative_workflow_call_audit_closure_invalid")
    if any(
        audit.audit_payload.get("prompt_version") != _NARRATIVE_PROMPT_VERSION
        for audit in audits
    ):
        raise NarrativeWorkflowError(
            "narrative_workflow_provider_audit_prompt_version_invalid"
        )
    if any(
        call_input.material_projection_ref != material_projection.projection_ref
        or call_input.material_projection_digest != material_projection.content_digest
        for call_input in call_inputs
    ):
        raise NarrativeWorkflowError(
            "narrative_workflow_provider_projection_closure_invalid"
        )
    response_refs = tuple(item.response_ref for item in responses)
    if (
        len(response_refs) != len(set(response_refs))
        or tuple(ref for audit in audits for ref in audit.provider_response_refs)
        != response_refs
    ):
        raise NarrativeWorkflowError("narrative_workflow_response_closure_invalid")
    responses_by_ref = {item.response_ref: item for item in responses}
    expected_initial_writer_payload = {
        "material_projection": material_projection.to_writer_payload(),
        "requirement_limitation_scope": _requirement_limitation_scope(
            material_projection=material_projection,
            blocks=(),
        ),
        "answer_context": answer_context.to_writer_payload(),
    }
    if canonical_value(call_inputs[0].payload) != canonical_value(
        expected_initial_writer_payload
    ):
        raise NarrativeWorkflowError(
            "narrative_workflow_initial_writer_input_closure_invalid"
        )
    raw_initial_writer_output = audits[0].audit_payload.get("structured_output")
    if not isinstance(raw_initial_writer_output, Mapping):
        raise NarrativeWorkflowError("narrative_workflow_initial_writer_output_invalid")
    normalized_initial_writer_output, initial_writer_findings = (
        _normalize_initial_writer_output_for_delivery(
            raw_initial_writer_output,
            authority_mode=authority_bundle.authority_mode,
            material_projection=material_projection,
        )
    )
    if audits[0].audit_payload.get(_WRITER_CONTRACT_FINDINGS_AUDIT_FIELD) != list(
        initial_writer_findings
    ) and audits[0].audit_payload.get(_WRITER_CONTRACT_FINDINGS_AUDIT_FIELD) != tuple(
        initial_writer_findings
    ):
        raise NarrativeWorkflowError(
            "narrative_workflow_writer_contract_findings_invalid"
        )
    initial_writer_responses = tuple(
        responses_by_ref[ref] for ref in audits[0].provider_response_refs
    )
    expected_initial_writer, expected_initial_narrative = (
        _initial_writer_attempt_and_document(
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            invocation=_ProviderInvocation(
                call_input=call_inputs[0],
                output=normalized_initial_writer_output,
                responses=initial_writer_responses,
                audit=audits[0],
            ),
        )
    )
    if (
        writers[0] != expected_initial_writer
        or narrative_versions[0] != expected_initial_narrative
    ):
        raise NarrativeWorkflowError(
            "narrative_workflow_initial_writer_normalization_closure_invalid"
        )
    for index in range(version_count):
        writer_call = call_inputs[index * 2]
        writer_audit = audits[index * 2]
        verifier_call = call_inputs[index * 2 + 1]
        verifier_audit = audits[index * 2 + 1]
        writer = writers[index]
        narrative = narrative_versions[index]
        local = local_versions[index]
        verifier_attempt = verifier_attempt_versions[index]
        verifier_report = verifier_report_versions[index]
        writer_response = responses_by_ref[writer_audit.provider_response_refs[-1]]
        verifier_response = responses_by_ref[verifier_audit.provider_response_refs[-1]]
        if (
            writer.input_ref != writer_call.call_input_ref
            or writer.input_digest != writer_call.content_digest
            or writer.material_projection_ref != material_projection.projection_ref
            or writer.material_projection_digest != material_projection.content_digest
            or writer.provider_response != writer_response
            or narrative.writer_attempt != writer
            or narrative.material_projection_ref != material_projection.projection_ref
            or narrative.material_projection_digest
            != material_projection.content_digest
            or local.narrative_id != narrative.narrative_id
            or local.narrative_digest != narrative.content_digest
            or local.material_projection_ref != material_projection.projection_ref
            or local.material_projection_digest != material_projection.content_digest
            or verifier_attempt.input_ref != verifier_call.call_input_ref
            or verifier_attempt.input_digest != verifier_call.content_digest
            or verifier_attempt.provider_response != verifier_response
            or verifier_attempt.narrative_id != narrative.narrative_id
            or verifier_attempt.local_report_ref != local.local_report_ref
            or verifier_report.verification_attempt != verifier_attempt
            or verifier_report.narrative_id != narrative.narrative_id
            or verifier_report.local_report_ref != local.local_report_ref
        ):
            raise NarrativeWorkflowError("narrative_workflow_version_closure_invalid")
        if index == 0:
            expected_verifier_payload, target_blocks, inherited_blocks = (
                _verifier_payload(
                    material_projection=material_projection,
                    answer_context=answer_context,
                    narrative=narrative,
                    local_report=local,
                )
            )
        else:
            expected_verifier_payload, target_blocks, inherited_blocks = (
                _verifier_payload(
                    material_projection=material_projection,
                    answer_context=answer_context,
                    narrative=narrative,
                    local_report=local,
                    source_narrative=narrative_versions[0],
                    source_local_report=local_versions[0],
                    source_verifier_report=verifier_report_versions[0],
                    source_verifier_call_input=call_inputs[1],
                )
            )
        if canonical_value(verifier_call.payload) != canonical_value(
            expected_verifier_payload
        ):
            raise NarrativeWorkflowError(
                "narrative_workflow_verifier_input_closure_invalid"
            )
        raw_verifier_output = verifier_audit.audit_payload.get("structured_output")
        if not isinstance(raw_verifier_output, Mapping):
            raise NarrativeWorkflowError("narrative_workflow_verifier_output_invalid")
        expected_verifier_report = _verifier_report_from_output(
            material_projection=material_projection,
            visibility_policy=visibility_policy,
            narrative=narrative,
            local_report=local,
            verification_attempt=verifier_attempt,
            output=raw_verifier_output,
            target_blocks=target_blocks,
            inherited_blocks=inherited_blocks,
        )
        if verifier_report != expected_verifier_report:
            raise NarrativeWorkflowError(
                "narrative_workflow_verifier_report_closure_invalid"
            )
    if version_count == 1 and focused_retry is not None:
        raise NarrativeWorkflowError("narrative_workflow_retry_cardinality_invalid")
    if version_count == 2 and (
        focused_retry is None
        or focused_retry.source_narrative_id != narrative_versions[0].narrative_id
        or focused_retry.rejection_report_ref
        != verifier_report_versions[0].verifier_report_ref
        or focused_retry.writer_attempt_ref != writers[1].writer_attempt_ref
        or focused_retry.resulting_narrative_id != narrative_versions[1].narrative_id
    ):
        raise NarrativeWorkflowError("narrative_workflow_retry_closure_invalid")
    if version_count == 2:
        if focused_retry is None:
            raise AssertionError("narrative_workflow_retry_missing")
        source_narrative = narrative_versions[0]
        source_report = verifier_report_versions[0]
        retry_plan = _compile_focused_retry_plan(
            source_order=source_narrative.blocks,
            accepted_block_ids=source_report.accepted_block_ids,
            rejected_block_ids=source_report.rejected_block_ids,
            material_projection=material_projection,
        )
        if focused_retry.targeted_block_ids != tuple(
            item.block_id for item in retry_plan.source_target_blocks
        ) or focused_retry.preserved_source_block_ids != tuple(
            item.block_id for item in retry_plan.preserved_blocks
        ):
            raise NarrativeWorkflowError(
                "narrative_workflow_retry_scope_closure_invalid"
            )
        expected_focused_payload = {
            "material_projection": _focused_scoped_material_view(
                retry_plan=retry_plan,
                material_projection=material_projection,
            ),
            "requirement_limitation_scope": (
                _focused_requirement_limitation_scope(
                    retry_plan=retry_plan,
                    material_projection=material_projection,
                )
            ),
            "answer_context": _focused_retry_answer_context_payload(
                answer_context=answer_context,
                source_narrative=source_narrative,
                rejection_report=source_report,
                local_report=local_versions[0],
                retry_plan=retry_plan,
            ),
        }
        if canonical_value(call_inputs[2].payload) != canonical_value(
            expected_focused_payload
        ):
            raise NarrativeWorkflowError(
                "narrative_workflow_focused_input_closure_invalid"
            )
        raw_focused_output = audits[2].audit_payload.get("structured_output")
        if not isinstance(raw_focused_output, Mapping):
            raise NarrativeWorkflowError("narrative_workflow_focused_output_invalid")
        _validated_focused_writer_merge(
            raw_focused_output,
            source_order=source_narrative.blocks,
            retry_plan=retry_plan,
            authority_mode=authority_bundle.authority_mode,
            material_projection=material_projection,
        )
        expected_result = _focused_narrative_from_target_output(
            authority_bundle=authority_bundle,
            material_projection=material_projection,
            source_narrative=source_narrative,
            writer_attempt=writers[1],
            target_output=raw_focused_output,
            retry_plan=retry_plan,
        )
        if narrative_versions[1] != expected_result:
            raise NarrativeWorkflowError(
                "narrative_workflow_focused_merge_closure_invalid"
            )
    final_narrative = narrative_versions[-1]
    final_local = local_versions[-1]
    final_verifier = verifier_report_versions[-1]
    final_blocks_by_id = {item.block_id: item for item in final_narrative.blocks}
    withheld = tuple(
        item
        for item in final_verifier.rejected_block_ids
        if final_blocks_by_id[item].required
    )
    # Verification is an audit signal for human review and later model/system
    # improvement. It must never regain authority over whether the generated
    # business reference is delivered to the user. Hard output-safety remains
    # enforced later by the customer projection.
    publication_ready = True
    body = {
        "authority_bundle_ref": authority_bundle.bundle_ref,
        "authority_bundle_digest": authority_bundle.bundle_digest,
        "claim_settlement_ref": claim_settlement.settlement_ref,
        "claim_settlement_digest": claim_settlement.content_digest,
        "public_materialization_ref": public_materialization.materialization_ref,
        "public_materialization_digest": public_materialization.content_digest,
        "visibility_policy_ref": visibility_policy.policy_ref,
        "visibility_policy_digest": visibility_policy.content_digest,
        "answer_context_ref": answer_context.context_ref,
        "answer_context_digest": answer_context.content_digest,
        "material_projection_ref": material_projection.projection_ref,
        "material_projection_digest": material_projection.content_digest,
        "provider_call_input_refs": tuple(
            item.call_input_ref for item in provider_call_inputs
        ),
        "provider_response_refs": tuple(
            item.response_ref for item in provider_responses
        ),
        "provider_audit_refs": tuple(item.audit_ref for item in provider_audits),
        "writer_attempt_refs": tuple(
            item.writer_attempt_ref for item in writer_attempts
        ),
        "narrative_ids": tuple(item.narrative_id for item in narratives),
        "local_report_refs": tuple(item.local_report_ref for item in local_reports),
        "verification_attempt_refs": tuple(
            item.verification_attempt_ref for item in verification_attempts
        ),
        "verifier_report_refs": tuple(
            item.verifier_report_ref for item in verifier_reports
        ),
        "focused_retry_ref": focused_retry.retry_ref if focused_retry else None,
        "final_accepted_narrative_id": final_narrative.narrative_id,
        "final_local_report_ref": final_local.local_report_ref,
        "projection_ready_verifier_report_ref": final_verifier.verifier_report_ref,
        "publication_ready": publication_ready,
        "withheld_required_block_ids": withheld,
    }
    return NarrativeWorkflowResult(
        authority_bundle_ref=authority_bundle.bundle_ref,
        authority_bundle_digest=authority_bundle.bundle_digest,
        claim_settlement_ref=claim_settlement.settlement_ref,
        claim_settlement_digest=claim_settlement.content_digest,
        public_materialization=public_materialization,
        visibility_policy=visibility_policy,
        answer_context=answer_context,
        material_projection=material_projection,
        provider_call_inputs=call_inputs,
        provider_responses=responses,
        provider_audits=audits,
        writer_attempts=writers,
        narratives=narrative_versions,
        local_reports=local_versions,
        verification_attempts=verifier_attempt_versions,
        verifier_reports=verifier_report_versions,
        focused_retry=focused_retry,
        final_accepted_narrative=final_narrative,
        final_local_report=final_local,
        projection_ready_verifier_report=final_verifier,
        publication_ready=publication_ready,
        withheld_required_block_ids=withheld,
        content_digest=canonical_digest(body),
    )


def validate_typed_narrative_workflow_result(
    value: NarrativeWorkflowResult,
    *,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    evidence_entries: Sequence[EvidenceLedgerEntry],
) -> NarrativeWorkflowResult:
    if type(value) is not NarrativeWorkflowResult:
        raise NarrativeWorkflowError("narrative_workflow_result_invalid")
    settlement = _validated_settlement(claim_settlement)
    _assert_bundle_settlement_closure(authority_bundle, settlement)
    try:
        materialization = ReviewedPublicFactMaterialization.from_dict(
            value.public_materialization.to_dict(),
            authority_bundle=authority_bundle,
            claim_settlement=settlement,
        )
        policy = PublicationFieldVisibilityPolicy.from_dict(
            value.visibility_policy.to_dict()
        )
        context = NarrativeAnswerContext.from_dict(value.answer_context.to_dict())
        normalized_recommendations = _typed_sequence(
            recommendations,
            RecommendationRecord,
            "recommendation_ref",
            "narrative_workflow_result_recommendations_invalid",
        )
        palette = PublicClaimPalette.derive(
            authority_bundle=authority_bundle,
            claims=settlement.accepted_claims,
            claim_keys=settlement.accepted_claim_keys,
            recommendations=normalized_recommendations,
            public_facts=materialization.public_facts,
            public_limitations=materialization.public_limitations,
            visibility_policy=policy,
        )
        material_projection = NarrativeMaterialProjection.derive(
            palette=palette,
            claim_settlement=settlement,
            evidence_entries=evidence_entries,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeWorkflowError(
            "narrative_workflow_result_authority_replay_invalid"
        ) from exc
    if material_projection != value.material_projection:
        raise NarrativeWorkflowError(
            "narrative_workflow_result_material_projection_invalid"
        )
    rebuilt = _workflow_result(
        authority_bundle=authority_bundle,
        claim_settlement=settlement,
        public_materialization=materialization,
        visibility_policy=policy,
        answer_context=context,
        material_projection=material_projection,
        provider_call_inputs=value.provider_call_inputs,
        provider_responses=value.provider_responses,
        provider_audits=value.provider_audits,
        writer_attempts=value.writer_attempts,
        narratives=value.narratives,
        local_reports=value.local_reports,
        verification_attempts=value.verification_attempts,
        verifier_reports=value.verifier_reports,
        focused_retry=value.focused_retry,
    )
    if rebuilt != value:
        raise NarrativeWorkflowError("narrative_workflow_result_integrity_invalid")
    return value


def _prepare_narrative_material_projection(
    *,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    evidence_entries: Sequence[EvidenceLedgerEntry],
    recommendations: Sequence[RecommendationRecord],
    public_materialization: ReviewedPublicFactMaterialization,
    visibility_policy: PublicationFieldVisibilityPolicy,
) -> tuple[
    ClaimSettlement,
    PublicationFieldVisibilityPolicy,
    PublicClaimPalette,
    NarrativeMaterialProjection,
]:
    """Rebuild the exact provider-facing material authority without I/O."""

    settlement = _validated_settlement(claim_settlement)
    _assert_bundle_settlement_closure(authority_bundle, settlement)
    if type(public_materialization) is not ReviewedPublicFactMaterialization:
        raise NarrativeWorkflowError("reviewed_public_fact_materialization_invalid")
    if (
        public_materialization.authority_bundle_ref != authority_bundle.bundle_ref
        or public_materialization.authority_bundle_digest
        != authority_bundle.bundle_digest
        or public_materialization.claim_settlement_ref != settlement.settlement_ref
        or public_materialization.claim_settlement_digest != settlement.content_digest
    ):
        raise NarrativeWorkflowError(
            "reviewed_public_fact_materialization_closure_invalid"
        )
    if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
        raise NarrativeWorkflowError("narrative_visibility_policy_invalid")
    try:
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeWorkflowError("narrative_visibility_policy_invalid") from exc
    normalized_recommendations = _typed_sequence(
        recommendations,
        RecommendationRecord,
        "recommendation_ref",
        "narrative_recommendations_invalid",
    )
    if tuple(item.recommendation_ref for item in normalized_recommendations) != (
        authority_bundle.recommendation_refs
    ):
        raise NarrativeWorkflowError("narrative_recommendation_closure_invalid")
    for recommendation in normalized_recommendations:
        try:
            replayed = RecommendationRecord.from_dict(
                recommendation.to_dict(),
                authority_namespace=settlement.authority_namespace,
                claim_settlement=settlement,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeWorkflowError("narrative_recommendation_invalid") from exc
        if replayed != recommendation:
            raise NarrativeWorkflowError("narrative_recommendation_invalid")
    palette = PublicClaimPalette.derive(
        authority_bundle=authority_bundle,
        claims=settlement.accepted_claims,
        claim_keys=settlement.accepted_claim_keys,
        recommendations=normalized_recommendations,
        public_facts=public_materialization.public_facts,
        public_limitations=public_materialization.public_limitations,
        visibility_policy=policy,
    )
    try:
        material_projection = NarrativeMaterialProjection.derive(
            palette=palette,
            claim_settlement=settlement,
            evidence_entries=evidence_entries,
        )
    except (TypeError, ValueError) as exc:
        raise NarrativeWorkflowError("narrative_material_projection_invalid") from exc
    return settlement, policy, palette, material_projection


def prepare_narrative_material_projection(
    *,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    evidence_entries: Sequence[EvidenceLedgerEntry],
    recommendations: Sequence[RecommendationRecord],
    public_materialization: ReviewedPublicFactMaterialization,
    visibility_policy: PublicationFieldVisibilityPolicy,
) -> tuple[PublicClaimPalette, NarrativeMaterialProjection]:
    """Prepare the content-addressed authority for a durable pre-provider checkpoint."""

    _, _, palette, material_projection = _prepare_narrative_material_projection(
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        evidence_entries=evidence_entries,
        recommendations=recommendations,
        public_materialization=public_materialization,
        visibility_policy=visibility_policy,
    )
    return palette, material_projection


def run_narrative_workflow(
    *,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    evidence_entries: Sequence[EvidenceLedgerEntry],
    recommendations: Sequence[RecommendationRecord],
    public_materialization: ReviewedPublicFactMaterialization,
    visibility_policy: PublicationFieldVisibilityPolicy,
    material_projection: NarrativeMaterialProjection,
    answer_context: NarrativeAnswerContext,
    llm_client: TypedNarrativeLLM,
    sensitive_output_inspector: SensitiveOutputInspector,
) -> NarrativeWorkflowResult:
    settlement, policy, _, expected_projection = _prepare_narrative_material_projection(
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        evidence_entries=evidence_entries,
        recommendations=recommendations,
        public_materialization=public_materialization,
        visibility_policy=visibility_policy,
    )
    if type(material_projection) is not NarrativeMaterialProjection:
        raise NarrativeWorkflowError("narrative_material_projection_invalid")
    try:
        material_projection.assert_integrity()
    except (TypeError, ValueError) as exc:
        raise NarrativeWorkflowError("narrative_material_projection_invalid") from exc
    if material_projection != expected_projection:
        raise NarrativeWorkflowError("narrative_material_projection_closure_invalid")
    if type(answer_context) is not NarrativeAnswerContext:
        raise NarrativeWorkflowError("narrative_answer_context_invalid")
    expected_context = NarrativeAnswerContext.create(
        user_question=answer_context.user_question,
        answer_goal=answer_context.answer_goal,
        locale=answer_context.locale,
        business_context=answer_context.business_context,
    )
    if expected_context != answer_context:
        raise NarrativeWorkflowError("narrative_answer_context_invalid")
    initial_payload = {
        "material_projection": material_projection.to_writer_payload(),
        "requirement_limitation_scope": _requirement_limitation_scope(
            material_projection=material_projection,
            blocks=(),
        ),
        "answer_context": answer_context.to_writer_payload(),
    }
    initial_input = NarrativeProviderCallInput.create(
        purpose="narrative_writer",
        authority_bundle=authority_bundle,
        material_projection=material_projection,
        payload=initial_payload,
    )

    def initial_validator(output: Mapping[str, Any]) -> None:
        _initial_writer_validator(
            output,
            authority_mode=authority_bundle.authority_mode,
            material_projection=material_projection,
        )

    writer_invocation = _invoke_provider(
        llm_client,
        call_input=initial_input,
        system_prompt=_WRITER_SYSTEM_PROMPT,
        required_key="blocks",
        validator=initial_validator,
        output_normalizer=lambda output: _normalize_initial_writer_output_for_delivery(
            output,
            authority_mode=authority_bundle.authority_mode,
            material_projection=material_projection,
        ),
    )
    writer_attempt, narrative = _initial_writer_attempt_and_document(
        authority_bundle=authority_bundle,
        material_projection=material_projection,
        invocation=writer_invocation,
    )
    findings = _sensitive_findings(
        sensitive_output_inspector,
        narrative=narrative,
        visibility_policy=policy,
    )
    local_report = BlockLocalValidationReport.validate(
        narrative=narrative,
        material_projection=material_projection,
        visibility_policy=policy,
        sensitive_output_findings=findings,
    )
    verifier_invocation, verification_attempt, verifier_report = _verify_narrative(
        authority_bundle=authority_bundle,
        material_projection=material_projection,
        visibility_policy=policy,
        answer_context=answer_context,
        narrative=narrative,
        local_report=local_report,
        llm_client=llm_client,
    )
    provider_call_inputs = [
        writer_invocation.call_input,
        verifier_invocation.call_input,
    ]
    provider_responses = [*writer_invocation.responses, *verifier_invocation.responses]
    provider_audits = [writer_invocation.audit, verifier_invocation.audit]
    writer_attempts = [writer_attempt]
    narratives = [narrative]
    local_reports = [local_report]
    verification_attempts = [verification_attempt]
    verifier_reports = [verifier_report]
    focused_retry: FocusedNarrativeRetry | None = None

    return _workflow_result(
        authority_bundle=authority_bundle,
        claim_settlement=settlement,
        public_materialization=public_materialization,
        visibility_policy=policy,
        answer_context=answer_context,
        material_projection=material_projection,
        provider_call_inputs=provider_call_inputs,
        provider_responses=provider_responses,
        provider_audits=provider_audits,
        writer_attempts=writer_attempts,
        narratives=narratives,
        local_reports=local_reports,
        verification_attempts=verification_attempts,
        verifier_reports=verifier_reports,
        focused_retry=focused_retry,
    )


__all__ = (
    "FocusedNarrativeRetry",
    "NarrativeAnswerContext",
    "NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT",
    "NarrativeProviderCallAudit",
    "NarrativeProviderCallError",
    "NarrativeProviderCallInput",
    "NarrativeWorkflowError",
    "NarrativeWorkflowResult",
    "ReviewedPublicFactMaterialization",
    "SensitiveOutputInspector",
    "TypedNarrativeLLM",
    "prepare_narrative_material_projection",
    "run_narrative_workflow",
    "validate_typed_narrative_workflow_result",
)
