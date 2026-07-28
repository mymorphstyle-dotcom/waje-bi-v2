from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.durable_tool_bridge import MaterialDecisionTopic


TOOL_SELECTION_SCHEMA_VERSION = "agent-turn-action-binding.v2"

TOOL_DISCOVERY_INSTRUCTIONS = """\
Bind the first executable action and select the smallest set of optional WAJE tools for the
current user turn. The action must be respond, call_tool, ask_user, or request_approval.
Use only tool names from the supplied catalog. Select no optional tool when the request can be
answered directly from ordinary conversation context. Select artifact tools for persisted-result
explanation, BI analysis tools only when new business-data evidence or a material revision is
needed, and the capability catalog tool when the user asks what analysis is available. Use
inspect_analysis_artifact or explain_claim directly for a follow-up about an existing
publication's calculation, evidence trace, meaning, or limitation. Both tools accept a bounded
list of exact references. Use artifactIndex.routing_summary and task_ref as a hierarchy: for a
broad recap or boundary question about one published analysis, select its newest bi_publication
alone. Select a bi_claim only when the question targets that claim more narrowly, and select
bi_evidence only when evidence-level detail absent from the publication or claim is required.
Never select a bi_publication together with its descendant claims or evidence in one call. Batch
only the smallest set of peer materials needed for the requested angle; do not issue a serial call
for every claim. Do not delegate that work.
Prior assistant prose is not material authority for those questions. Call the artifact or claim
tool even when the same statement or number is already visible in conversation context, so the
turn carries the persisted authority and material references.
For artifact tools, copy the exact artifact_ref or claim reference from artifactIndex. Never use
an artifact version, invent a type prefix, or transform an opaque reference. Routing summaries
only help select references; facts and conclusions still come from the tool result.
conversationContext.publishedAnalysisTasks is the typed list of published BI tasks available as
revision sources. When the latest message materially changes the metric, time window, baseline,
scope, filters, analysis axes, goal, or desired decisions of one supplied published task while
continuing that investigation, choose continue_bi_analysis and copy its taskRef as sourceTaskRef.
Choose run_bi_analysis for an independent new investigation that does not revise a supplied
published task. Never guess a revision source identifier from prose or opaque artifact refs.
delegate_independent_investigations is reserved for work that genuinely requires separate,
independently scoped investigation artifacts, such as competing hypotheses or independent report
sections. It is not a higher-quality replacement for direct artifact inspection or claim
explanation.
ask_user when material ambiguity can change the business conclusion, baseline, time semantics,
evidence use, claim strength, fixed sensitive output, data access, or material execution cost.
Before choosing a data-producing tool, assess whether the requested metric, comparison scope,
time window, baseline or counterfactual, and requested claim strength are sufficiently bound by
the current message and supplied context. If any missing choice can materially change the answer,
choose ask_user. Do not silently replace vague time expressions, undefined notions of "better",
an unspecified comparison group, or a missing causal baseline with a convenient default. Low-risk
presentation details may proceed without clarification. Treat an explicitly named business
measure plus explicitly compared periods or cohorts as bound for descriptive decomposition and
outlier-sensitivity analysis. A causal counterfactual is required for causal attribution; do not
invent that requirement for a descriptive comparison.
baseline_or_counterfactual identifies the causal reference condition: what would have happened
without the event, intervention, or treatment. comparison_scope identifies which business
entities, cohorts, or descriptive aggregation are compared. Do not encode one missing causal
reference as both topics. When the unresolved choice is only the absent-event condition or causal
baseline, emit baseline_or_counterfactual alone. These two topics are mutually exclusive in one
action binding; use baseline_or_counterfactual for causal questions and comparison_scope for
descriptive entity, cohort, or aggregation choices.
Treat an explicitly named reference period or reference population as a bound baseline, even when
its membership or aggregation is broad. Uncertainty about which members to include, whether to
compare them individually, or which aggregate to use belongs to comparison_scope only; it must
not reopen baseline_or_counterfactual or replace the stated reference population.
When a descriptive comparison names a multi-member reference population but omits the comparison
operator or aggregation, comparison_scope is material because total, mean, distribution, and
member-by-member comparisons can reverse the conclusion. You must choose ask_user for that scope.
For a descriptive outlier, concentration, robustness, or sensitivity question, the reviewed BI
capability contract chooses the statistical method and reports its limitations. Do not ask the
customer to choose an evidence type, threshold, or statistical technique. Mark evidence_use as
unresolved only when the customer explicitly asks to include, exclude, replace, or privilege a
class of evidence and that choice can materially change the conclusion.
Use conversationContext.businessClock as the authoritative current business date and timezone.
When a month is named without a year and it has one unambiguous most-recent completed occurrence
before currentDate, treat that year as a low-risk time inference and do not reopen time_window.
The downstream decision and plan authority must persist that inference. Ask about time only when
multiple plausible calendar interpretations can materially change the business conclusion.
Treat conversationContext.resolvedPendingActions as accepted authority. Topics listed in
resolvedMaterialDecisionTopics have already been resolved by the customer's answer and must not
be emitted again unless the latest message explicitly changes or rejects that choice. Acceptance
of a recommended option is sufficient; do not ask a second confirmation question that merely
restates the accepted metric, time window, comparison group, or baseline. When the accepted
decision closes the last material ambiguity, choose the applicable data-producing tool.
CAUSAL TOPIC EXCLUSIVITY IS MANDATORY. For an impact question of the form "how much did
an event, campaign, or intervention change a metric", a missing absent-event condition is
baseline_or_counterfactual. The fact that a counterfactual involves a comparison does not make
comparison_scope unresolved. Do not add comparison_scope to the same causal action binding. If
the scope of channels, regions, products, or customer segments is independently unresolved, bind
that decision after the causal reference has been resolved.
Always include requiredToolName and requiredToolArgumentsJson. Set both to null for respond,
ask_user, and request_approval; the typed runtime binds the latter two fixed actions to their
mandatory tools. For call_tool, requiredToolName must name the exact first optional tool and
requiredToolArgumentsJson must be a serialized JSON object that satisfies that tool's
inputSchema. Copy opaque references exactly from the supplied context. Do not infer
permissions or invent tools. Always populate materialDecisionTopics with every unresolved
material dimension from the allowed enum; leave it empty only when those dimensions are bound.
Any non-empty materialDecisionTopics means the first action is ask_user. Return only the typed
action binding output.
"""

class AgentToolDiscoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DynamicToolSelectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    selected_tools: list[str] = Field(alias="selectedTools")
    initial_action: Literal[
        "respond", "call_tool", "ask_user", "request_approval"
    ] = Field(alias="initialAction")
    required_tool_name: str | None = Field(alias="requiredToolName")
    required_tool_arguments_json: str | None = Field(
        alias="requiredToolArgumentsJson"
    )
    material_decision_topics: list[MaterialDecisionTopic] = Field(
        alias="materialDecisionTopics"
    )

    @field_validator("selected_tools")
    @classmethod
    def validate_selected_tools(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("agent_tool_selection_name_invalid")
        if len(values) != len(set(values)):
            raise ValueError("agent_tool_selection_name_duplicate")
        return values

    @model_validator(mode="after")
    def validate_action(self) -> "DynamicToolSelectionOutput":
        if {
            "baseline_or_counterfactual",
            "comparison_scope",
        }.issubset(self.material_decision_topics):
            raise ValueError("agent_tool_selection_causal_topic_overlap")
        if self.initial_action == "respond":
            if (
                self.required_tool_name is not None
                or self.required_tool_arguments_json is not None
                or self.selected_tools
                or self.material_decision_topics
            ):
                raise ValueError("agent_direct_action_tools_forbidden")
            return self
        if self.initial_action == "ask_user":
            if (
                self.required_tool_name is not None
                or self.required_tool_arguments_json is not None
                or not self.material_decision_topics
            ):
                raise ValueError("agent_clarification_tool_invalid")
            return self
        if self.initial_action == "request_approval":
            if (
                self.required_tool_name is not None
                or self.required_tool_arguments_json is not None
                or self.material_decision_topics
            ):
                raise ValueError("agent_approval_tool_invalid")
            return self
        if self.initial_action == "call_tool":
            if (
                not self.required_tool_name
                or self.required_tool_name not in self.selected_tools
                or self.required_tool_arguments_json is None
                or self.material_decision_topics
            ):
                raise ValueError("agent_required_action_tool_missing")
            if self.required_tool_name in {"ask_user", "request_approval"}:
                raise ValueError("agent_call_tool_action_invalid")
        return self


class AgentTurnActionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["agent-turn-action-binding.v2"] = Field(
        alias="schemaVersion",
        default=TOOL_SELECTION_SCHEMA_VERSION,
    )
    catalog_digest: str = Field(alias="catalogDigest", min_length=1)
    input_digest: str = Field(alias="inputDigest", min_length=1)
    action_context_digest: str = Field(alias="actionContextDigest", min_length=1)
    selected_tools: list[str] = Field(alias="selectedTools")
    initial_action: Literal[
        "respond", "call_tool", "ask_user", "request_approval"
    ] = Field(alias="initialAction")
    required_tool_name: str | None = Field(alias="requiredToolName")
    required_tool_arguments: dict[str, Any] | None = Field(
        alias="requiredToolArguments"
    )
    material_decision_topics: list[MaterialDecisionTopic] = Field(
        alias="materialDecisionTopics"
    )
    selection_digest: str = Field(alias="selectionDigest", min_length=1)

    @field_validator("selected_tools")
    @classmethod
    def validate_selected_tools(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("agent_tool_selection_name_invalid")
        if values != sorted(set(values)):
            raise ValueError("agent_tool_selection_names_not_canonical")
        return values

    @model_validator(mode="after")
    def validate_digest(self) -> "AgentTurnActionBinding":
        if self.schema_version != TOOL_SELECTION_SCHEMA_VERSION:
            raise ValueError("agent_tool_selection_schema_invalid")
        if self.initial_action == "respond":
            if (
                self.required_tool_name is not None
                or self.required_tool_arguments is not None
            ):
                raise ValueError("agent_direct_action_tools_forbidden")
        elif (
            not self.required_tool_name
            or self.required_tool_name not in self.selected_tools
        ):
            raise ValueError("agent_required_action_tool_missing")
        if self.initial_action == "call_tool":
            if self.required_tool_arguments is None:
                raise ValueError("agent_required_action_arguments_missing")
        elif self.required_tool_arguments is not None:
            raise ValueError("agent_action_arguments_forbidden")
        if self.initial_action == "ask_user" and self.required_tool_name != "ask_user":
            raise ValueError("agent_clarification_tool_invalid")
        if (
            self.initial_action == "request_approval"
            and self.required_tool_name != "request_approval"
        ):
            raise ValueError("agent_approval_tool_invalid")
        if self.initial_action == "call_tool" and self.required_tool_name in {
            "ask_user",
            "request_approval",
        }:
            raise ValueError("agent_call_tool_action_invalid")
        if self.material_decision_topics and self.initial_action != "ask_user":
            raise ValueError("agent_material_decision_action_invalid")
        expected = _selection_digest(
            catalog_digest=self.catalog_digest,
            input_digest=self.input_digest,
            action_context_digest=self.action_context_digest,
            selected_tools=self.selected_tools,
            initial_action=self.initial_action,
            required_tool_name=self.required_tool_name,
            required_tool_arguments=self.required_tool_arguments,
            material_decision_topics=self.material_decision_topics,
        )
        if self.selection_digest != expected:
            raise ValueError("agent_tool_selection_digest_invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        catalog_digest: str,
        input_digest: str,
        action_context_digest: str,
        selected_tools: Sequence[str],
        initial_action: str,
        required_tool_name: str | None,
        required_tool_arguments: Mapping[str, Any] | None,
        material_decision_topics: Sequence[str],
    ) -> "AgentTurnActionBinding":
        normalized = sorted(set(selected_tools))
        normalized_arguments = (
            canonical_value(dict(required_tool_arguments))
            if required_tool_arguments is not None
            else None
        )
        if normalized_arguments is not None and not isinstance(
            normalized_arguments, dict
        ):
            raise ValueError("agent_required_action_arguments_invalid")
        return cls(
            catalogDigest=catalog_digest,
            inputDigest=input_digest,
            actionContextDigest=action_context_digest,
            selectedTools=normalized,
            initialAction=initial_action,
            requiredToolName=required_tool_name,
            requiredToolArguments=normalized_arguments,
            materialDecisionTopics=sorted(set(material_decision_topics)),
            selectionDigest=_selection_digest(
                catalog_digest=catalog_digest,
                input_digest=input_digest,
                action_context_digest=action_context_digest,
                selected_tools=normalized,
                initial_action=initial_action,
                required_tool_name=required_tool_name,
                required_tool_arguments=normalized_arguments,
                material_decision_topics=material_decision_topics,
            ),
        )

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ToolSelectionAdapter(Protocol):
    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult: ...


class ToolSelectionGenerator(Protocol):
    async def select(
        self,
        *,
        user_message: str,
        tool_catalog: Sequence[Mapping[str, Any]],
        tool_input_models: Mapping[str, type[BaseModel]] | None = None,
        permission_scope: Mapping[str, Any],
        action_context: Mapping[str, Any],
    ) -> DynamicToolSelectionOutput: ...


class WajeToolSelectionGenerator:
    def __init__(
        self,
        adapter: ToolSelectionAdapter,
        *,
        trace_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._adapter = adapter
        self._trace_metadata = canonical_value(trace_metadata or {})

    async def select(
        self,
        *,
        user_message: str,
        tool_catalog: Sequence[Mapping[str, Any]],
        tool_input_models: Mapping[str, type[BaseModel]] | None = None,
        permission_scope: Mapping[str, Any],
        action_context: Mapping[str, Any] | None = None,
    ) -> DynamicToolSelectionOutput:
        base_payload = {
            "userMessage": user_message,
            "optionalToolCatalog": [dict(item) for item in tool_catalog],
            "permissionScope": canonical_value(permission_scope),
            "conversationContext": canonical_value(action_context or {}),
        }
        input_digest = canonical_digest(base_payload)
        validation_feedback: Mapping[str, Any] | None = None
        maximum_attempts = 3 if tool_input_models is not None else 1
        last_error: AgentToolDiscoveryError | None = None
        for attempt_number in range(1, maximum_attempts + 1):
            payload = dict(base_payload)
            if validation_feedback is not None:
                payload["bindingValidation"] = validation_feedback
            result = await self._adapter.run(
                WajeAgentRunRequest(
                    run_id=(
                        f"tool-selection-run-{input_digest[:24]}"
                        f"-attempt-{attempt_number}"
                    ),
                    agent_name="WAJE Dynamic Tool Discovery",
                    instructions=TOOL_DISCOVERY_INSTRUCTIONS,
                    input_text=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    output_type=DynamicToolSelectionOutput,
                    max_turns=1,
                    thinking_mode="disabled",
                    trace_metadata={
                        **self._trace_metadata,
                        "waje_tool_catalog_digest": canonical_digest(tool_catalog),
                        "waje_tool_selection_input_digest": input_digest,
                        "waje_tool_selection_attempt_number": attempt_number,
                    },
                )
            )
            output = DynamicToolSelectionOutput.model_validate(result.final_output)
            if tool_input_models is None:
                return output
            try:
                _validate_provider_selection_binding(
                    output,
                    tool_input_models=tool_input_models,
                )
            except AgentToolDiscoveryError as exc:
                last_error = exc
                validation_feedback = {
                    "status": "retry_required",
                    "failureCode": exc.code,
                    "instruction": (
                        "Return a corrected action binding whose selected tool and "
                        "requiredToolArgumentsJson satisfy the supplied catalog."
                    ),
                }
                continue
            return output
        raise last_error or AgentToolDiscoveryError(
            "agent_tool_selection_validation_failed"
        )


@dataclass(frozen=True)
class ResolvedAgentTools:
    tools: tuple[WajeAgentTool, ...]
    selection: AgentTurnActionBinding


class DynamicAgentToolResolver:
    def __init__(
        self,
        *,
        generator: ToolSelectionGenerator,
        mandatory_tool_names: Sequence[str],
        max_optional_tools: int = 4,
    ) -> None:
        mandatory = tuple(dict.fromkeys(mandatory_tool_names))
        if (
            not mandatory
            or any(not name or name != name.strip() for name in mandatory)
            or isinstance(max_optional_tools, bool)
            or max_optional_tools < 1
        ):
            raise ValueError("agent_tool_resolver_config_invalid")
        self._generator = generator
        self._mandatory_tool_names = frozenset(mandatory)
        self._max_optional_tools = max_optional_tools

    async def resolve(
        self,
        *,
        user_message: str,
        candidate_tools: Sequence[WajeAgentTool],
        permission_scope: Mapping[str, Any],
        action_context: Mapping[str, Any] | None = None,
    ) -> ResolvedAgentTools:
        catalog, by_name = _catalog(candidate_tools)
        missing_mandatory = self._mandatory_tool_names - set(by_name)
        if missing_mandatory:
            raise AgentToolDiscoveryError("agent_tool_mandatory_missing")
        optional_catalog = tuple(
            item for item in catalog if item["name"] not in self._mandatory_tool_names
        )
        output = await self._generator.select(
            user_message=user_message,
            tool_catalog=optional_catalog,
            tool_input_models={
                name: tool.input_model
                for name, tool in by_name.items()
                if name not in self._mandatory_tool_names
            },
            permission_scope=permission_scope,
            action_context=action_context or {},
        )
        optional_names = set(output.selected_tools)
        known_optional = {str(item["name"]) for item in optional_catalog}
        if not optional_names.issubset(known_optional):
            raise AgentToolDiscoveryError("agent_tool_selection_unknown")
        if len(optional_names) > self._max_optional_tools:
            raise AgentToolDiscoveryError("agent_tool_selection_limit_exceeded")
        selected_names = optional_names | self._mandatory_tool_names
        required_tool_name = (
            "ask_user"
            if output.initial_action == "ask_user"
            else "request_approval"
            if output.initial_action == "request_approval"
            else output.required_tool_name
        )
        if required_tool_name not in selected_names and required_tool_name is not None:
            raise AgentToolDiscoveryError("agent_action_required_tool_unselected")
        catalog_digest = canonical_digest(catalog)
        input_digest = _input_digest(user_message, permission_scope)
        required_tool_arguments = _validated_required_tool_arguments(
            initial_action=output.initial_action,
            required_tool_name=required_tool_name,
            raw_arguments=_decode_required_tool_arguments(
                output.required_tool_arguments_json
            ),
            tools_by_name=by_name,
        )
        _validate_required_tool_argument_authority(
            required_tool_name=required_tool_name,
            required_tool_arguments=required_tool_arguments,
            tools_by_name=by_name,
            action_context=action_context or {},
        )
        selection = AgentTurnActionBinding.create(
            catalog_digest=catalog_digest,
            input_digest=input_digest,
            action_context_digest=canonical_digest(action_context or {}),
            selected_tools=selected_names,
            initial_action=output.initial_action,
            required_tool_name=required_tool_name,
            required_tool_arguments=required_tool_arguments,
            material_decision_topics=output.material_decision_topics,
        )
        return ResolvedAgentTools(
            tools=tuple(tool for tool in candidate_tools if tool.name in selected_names),
            selection=selection,
        )

    def replay(
        self,
        *,
        user_message: str,
        candidate_tools: Sequence[WajeAgentTool],
        permission_scope: Mapping[str, Any],
        selection_payload: Mapping[str, Any],
        action_context: Mapping[str, Any] | None = None,
    ) -> ResolvedAgentTools:
        try:
            selection = AgentTurnActionBinding.model_validate(selection_payload)
        except Exception as exc:
            raise AgentToolDiscoveryError(
                "agent_tool_selection_payload_invalid"
            ) from exc
        catalog, by_name = _catalog(candidate_tools)
        if selection.catalog_digest != canonical_digest(catalog):
            raise AgentToolDiscoveryError("agent_tool_selection_catalog_conflict")
        if selection.input_digest != _input_digest(user_message, permission_scope):
            raise AgentToolDiscoveryError("agent_tool_selection_input_conflict")
        if selection.action_context_digest != canonical_digest(action_context or {}):
            raise AgentToolDiscoveryError(
                "agent_tool_selection_action_context_conflict"
            )
        selected_names = set(selection.selected_tools)
        if not self._mandatory_tool_names.issubset(selected_names):
            raise AgentToolDiscoveryError("agent_tool_selection_mandatory_missing")
        if not selected_names.issubset(set(by_name)):
            raise AgentToolDiscoveryError("agent_tool_selection_unknown")
        if (
            len(selected_names - self._mandatory_tool_names)
            > self._max_optional_tools
        ):
            raise AgentToolDiscoveryError("agent_tool_selection_limit_exceeded")
        if (
            selection.required_tool_name is not None
            and selection.required_tool_name not in selected_names
        ):
            raise AgentToolDiscoveryError("agent_action_required_tool_unselected")
        replayed_arguments = _validated_required_tool_arguments(
            initial_action=selection.initial_action,
            required_tool_name=selection.required_tool_name,
            raw_arguments=selection.required_tool_arguments,
            tools_by_name=by_name,
        )
        if replayed_arguments != selection.required_tool_arguments:
            raise AgentToolDiscoveryError(
                "agent_required_action_arguments_conflict"
            )
        _validate_required_tool_argument_authority(
            required_tool_name=selection.required_tool_name,
            required_tool_arguments=replayed_arguments,
            tools_by_name=by_name,
            action_context=action_context or {},
        )
        return ResolvedAgentTools(
            tools=tuple(tool for tool in candidate_tools if tool.name in selected_names),
            selection=selection,
        )


def _catalog(
    tools: Sequence[WajeAgentTool],
) -> tuple[tuple[dict[str, Any], ...], dict[str, WajeAgentTool]]:
    by_name = {tool.name: tool for tool in tools}
    if len(by_name) != len(tools):
        raise AgentToolDiscoveryError("agent_tool_catalog_name_duplicate")
    catalog = tuple(
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_model.model_json_schema(),
            "executionMode": tool.execution_mode,
            "prebindingPolicy": tool.prebinding_policy,
            "argumentAuthorityValidation": (
                tool.argument_authority_validator is not None
            ),
        }
        for tool in sorted(tools, key=lambda item: item.name)
    )
    return catalog, by_name


def _input_digest(
    user_message: str,
    permission_scope: Mapping[str, Any],
) -> str:
    return canonical_digest(
        {
            "user_message": user_message,
            "permission_scope": canonical_value(permission_scope),
        }
    )


def _selection_digest(
    *,
    catalog_digest: str,
    input_digest: str,
    action_context_digest: str,
    selected_tools: Sequence[str],
    initial_action: str,
    required_tool_name: str | None,
    required_tool_arguments: Mapping[str, Any] | None,
    material_decision_topics: Sequence[str],
) -> str:
    return canonical_digest(
        {
            "schema_version": TOOL_SELECTION_SCHEMA_VERSION,
            "catalog_digest": catalog_digest,
            "input_digest": input_digest,
            "action_context_digest": action_context_digest,
            "selected_tools": sorted(set(selected_tools)),
            "initial_action": initial_action,
            "required_tool_name": required_tool_name,
            "required_tool_arguments": (
                canonical_value(dict(required_tool_arguments))
                if required_tool_arguments is not None
                else None
            ),
            "material_decision_topics": sorted(set(material_decision_topics)),
        }
    )


def _validated_required_tool_arguments(
    *,
    initial_action: str,
    required_tool_name: str | None,
    raw_arguments: Mapping[str, Any] | None,
    tools_by_name: Mapping[str, WajeAgentTool],
) -> dict[str, Any] | None:
    if initial_action != "call_tool":
        if raw_arguments is not None:
            raise AgentToolDiscoveryError("agent_action_arguments_forbidden")
        return None
    if required_tool_name is None or raw_arguments is None:
        raise AgentToolDiscoveryError("agent_required_action_arguments_missing")
    tool = tools_by_name.get(required_tool_name)
    if tool is None:
        raise AgentToolDiscoveryError("agent_action_required_tool_unselected")
    try:
        parsed = tool.input_model.model_validate(dict(raw_arguments))
    except Exception as exc:
        raise AgentToolDiscoveryError(
            "agent_required_action_arguments_invalid"
        ) from exc
    normalized = canonical_value(parsed.model_dump(mode="json"))
    if not isinstance(normalized, dict):
        raise AgentToolDiscoveryError("agent_required_action_arguments_invalid")
    return normalized


def _validate_provider_selection_binding(
    output: DynamicToolSelectionOutput,
    *,
    tool_input_models: Mapping[str, type[BaseModel]],
) -> None:
    known = set(tool_input_models)
    if not set(output.selected_tools).issubset(known):
        raise AgentToolDiscoveryError("agent_tool_selection_unknown")
    if output.initial_action != "call_tool":
        return
    name = output.required_tool_name
    if name is None or name not in tool_input_models:
        raise AgentToolDiscoveryError("agent_action_required_tool_unselected")
    raw_arguments = _decode_required_tool_arguments(
        output.required_tool_arguments_json
    )
    if raw_arguments is None:
        raise AgentToolDiscoveryError("agent_required_action_arguments_missing")
    try:
        tool_input_models[name].model_validate(dict(raw_arguments))
    except Exception as exc:
        raise AgentToolDiscoveryError(
            "agent_required_action_arguments_invalid"
        ) from exc


def _decode_required_tool_arguments(
    value: str | None,
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not value or value != value.strip():
        raise AgentToolDiscoveryError("agent_required_action_arguments_json_invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AgentToolDiscoveryError(
            "agent_required_action_arguments_json_invalid"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise AgentToolDiscoveryError("agent_required_action_arguments_json_invalid")
    return decoded


def _validate_required_tool_argument_authority(
    *,
    required_tool_name: str | None,
    required_tool_arguments: Mapping[str, Any] | None,
    tools_by_name: Mapping[str, WajeAgentTool],
    action_context: Mapping[str, Any],
) -> None:
    if required_tool_name is None or required_tool_arguments is None:
        return
    validator = tools_by_name[required_tool_name].argument_authority_validator
    if validator is None:
        return
    try:
        validator(required_tool_arguments, action_context)
    except Exception as exc:
        raise AgentToolDiscoveryError(
            "agent_required_action_argument_authority_invalid"
        ) from exc


__all__ = (
    "AgentTurnActionBinding",
    "AgentToolDiscoveryError",
    "DynamicAgentToolResolver",
    "DynamicToolSelectionOutput",
    "ResolvedAgentTools",
    "TOOL_DISCOVERY_INSTRUCTIONS",
    "TOOL_SELECTION_SCHEMA_VERSION",
    "ToolSelectionGenerator",
    "WajeToolSelectionGenerator",
)
