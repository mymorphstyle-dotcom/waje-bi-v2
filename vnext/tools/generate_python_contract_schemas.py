#!/usr/bin/env python3
"""Generate strict transport schemas from the epoch-3 Python contracts."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from waje_vnext.domain.actions import (
    ACTION_PAYLOAD_TYPES,
    ActionKind,
)
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    InvestigationCase,
    WorkPlanRevision,
)
from waje_vnext.domain.answering import (
    AnalysisCheckDisposition,
    AnswerVersion,
    ClaimPrecheckRecord,
    ProvisionalAnswerCandidate,
    SettlementPreconditionReport,
)
from waje_vnext.domain.canonical import FrozenJson
from waje_vnext.domain.context import ContextPacket
from waje_vnext.domain.evidence import (
    CapabilityResultEnvelope,
    CapabilityResultReceipt,
    EvidenceAdmissionRecord,
    EvidenceRecord,
    EvidenceUseBinding,
    EvidenceValidityRecord,
    ObligationSatisfactionRecord,
)
from waje_vnext.domain.measurement import (
    AnalysisFrameRevision as Epoch3AnalysisFrameRevision,
    MeasurementResolutionOutcome,
    QuestionRevision,
    ResolvedEvidenceObligation,
)
from waje_vnext.domain.planning import (
    ConformanceExecutionSpec,
    LogicalExecutionAttempt,
    PlanAdoptionRecord,
    QueryBindingEnvelope,
)
from waje_vnext.domain.runtime_amendment import (
    DispatcherRecoveryCursor,
    FrameAdmissionProof,
    FrameCandidateRecord,
    FrameReviewRecord,
    JobDispositionRecord,
    LogicalModelJob,
    MessageBindingRequest,
    MessageImpactBinding,
    MessageImpactProposal,
    MessageIngressRecord,
    ObjectionClosureRecord,
    PendingUserMessage,
    DurableModelResult,
    ModelConfigurationIdentity,
    ModelRequestArtifact,
    ProviderAttemptReceipt,
    ProviderAttemptRequest,
    RunTraceManifest,
)
from waje_vnext.domain.workflow import (
    WorkflowApplicationReceipt,
    WorkflowProjectionHead,
    WorkflowReadModel,
    WorkflowSnapshot,
)


ROOT = Path(__file__).resolve().parents[1]
DOMAIN = ROOT / "contracts" / "domain"
JSON_VALUE_REF = {"$ref": "#/$defs/jsonValue"}
SHA256_PATTERN = "^[0-9a-f]{64}$"
EPOCH_3_RECORDS = {
    QuestionRevision,
    Epoch3AnalysisFrameRevision,
    MeasurementResolutionOutcome,
    ResolvedEvidenceObligation,
    EvidenceValidityRecord,
    ObligationSatisfactionRecord,
    SettlementPreconditionReport,
    PlanAdoptionRecord,
    QueryBindingEnvelope,
    ConformanceExecutionSpec,
    LogicalExecutionAttempt,
    CapabilityResultEnvelope,
    CapabilityResultReceipt,
    EvidenceRecord,
    EvidenceAdmissionRecord,
    EvidenceValidityRecord,
    EvidenceUseBinding,
    ObligationSatisfactionRecord,
    AnalysisCheckDisposition,
    ProvisionalAnswerCandidate,
    ClaimPrecheckRecord,
    AnswerVersion,
    SettlementPreconditionReport,
    WorkflowSnapshot,
    WorkflowApplicationReceipt,
    WorkflowProjectionHead,
}
SHA256_FIELDS = {
    "authority_binding_id",
    "authority_snapshot_sha256",
    "claim_target_spec_sha256",
    "closure_definition_sha256",
    "conformance_execution_spec_id",
    "content_sha256",
    "derived_input_sha256",
    "derivation_proof_sha256",
    "evidence_requirement_sha256",
    "execution_spec_content_sha256",
    "expected_prior_content_sha256",
    "field_derivation_proof_sha256",
    "fixture_content_sha256",
    "frame_content_sha256",
    "frame_review_content_sha256",
    "input_set_sha256",
    "input_sha256",
    "lineage_sha256",
    "logical_execution_attempt_id",
    "logical_execution_id",
    "message_payload_sha256",
    "obligation_id",
    "obligation_content_sha256",
    "payload_sha256",
    "plan_adoption_id",
    "plan_content_sha256",
    "prior_attempt_id",
    "proposed_frame_content_sha256",
    "query_binding_content_sha256",
    "query_binding_id",
    "request_sha256",
    "reviewed_frame_content_sha256",
    "resolution_id",
    "resolution_outcome_content_sha256",
    "resolution_outcome_id",
    "selected_text_sha256",
    "semantic_binding_sha256",
    "semantic_measurement_id",
    "source_payload_sha256",
    "output_sha256",
}
SHA256_ARRAY_FIELDS = {
    "authority_binding_ids",
    "semantic_measurement_ids",
}
RECORD_SHA256_ARRAY_FIELDS = {
    "PlanAdoptionRecord": {
        "resolution_outcome_ids": True,
        "resolution_outcome_content_sha256s": True,
        "resolution_admission_content_sha256s": True,
        "resolution_context_sha256s": True,
        "resolver_input_bundle_sha256s": True,
        "resolution_registry_content_sha256s": True,
        "obligation_ids": True,
        "obligation_content_sha256s": True,
        "query_binding_ids": False,
        "query_binding_content_sha256s": False,
    },
    "WorkPlanRevision": {
        "resolution_outcome_ids": True,
    },
    "WorkTask": {
        "obligation_ids": True,
        "query_binding_ids": False,
    },
}
NON_UNIQUE_RECORD_SHA256_ARRAY_FIELDS = {
    "PlanAdoptionRecord": {
        "resolution_outcome_content_sha256s",
        "resolution_admission_content_sha256s",
        "resolution_context_sha256s",
        "resolver_input_bundle_sha256s",
        "resolution_registry_content_sha256s",
        "obligation_content_sha256s",
        "query_binding_content_sha256s",
    },
}


class SchemaBuilder:
    def __init__(self) -> None:
        self.definitions: dict[str, object] = {
            "jsonValue": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "boolean"},
                    {"type": "integer"},
                    {"type": "number"},
                    {"type": "string"},
                    {
                        "type": "array",
                        "items": {"$ref": "#/$defs/jsonValue"},
                    },
                    {
                        "type": "object",
                        "additionalProperties": {
                            "$ref": "#/$defs/jsonValue"
                        },
                    },
                ]
            }
        }
        self._building: set[type[object]] = set()

    def ref(self, record_type: type[object]) -> dict[str, str]:
        name = record_type.__name__
        if name not in self.definitions:
            self._build_dataclass(record_type)
        return {"$ref": f"#/$defs/{name}"}

    def schema_for(self, expected_type: object) -> dict[str, object]:
        if expected_type is FrozenJson or expected_type in (Any, object):
            return dict(JSON_VALUE_REF)
        if isinstance(expected_type, TypeAliasType):
            return self.schema_for(expected_type.__value__)
        origin = get_origin(expected_type)
        arguments = get_args(expected_type)
        if origin in (types.UnionType, Union):
            return {
                "anyOf": [
                    self.schema_for(candidate)
                    for candidate in arguments
                ]
            }
        if origin is tuple:
            if len(arguments) == 2 and arguments[1] is Ellipsis:
                return {
                    "type": "array",
                    "items": self.schema_for(arguments[0]),
                }
            return {
                "type": "array",
                "prefixItems": [
                    self.schema_for(candidate)
                    for candidate in arguments
                ],
                "minItems": len(arguments),
                "maxItems": len(arguments),
            }
        if origin in (dict, Mapping):
            return {
                "type": "object",
                "additionalProperties": self.schema_for(arguments[1]),
            }
        if expected_type is type(None):
            return {"type": "null"}
        if expected_type is str:
            return {"type": "string"}
        if expected_type is bool:
            return {"type": "boolean"}
        if expected_type is int:
            return {"type": "integer"}
        if expected_type is float:
            return {"type": "number"}
        if expected_type is datetime:
            return {"type": "string", "format": "date-time"}
        if expected_type is date:
            return {"type": "string", "format": "date"}
        if (
            isinstance(expected_type, type)
            and issubclass(expected_type, Enum)
        ):
            return {
                "type": "string",
                "enum": [member.value for member in expected_type],
            }
        if (
            isinstance(expected_type, type)
            and is_dataclass(expected_type)
        ):
            return self.ref(expected_type)
        raise TypeError(f"unsupported schema type: {expected_type!r}")

    def _build_dataclass(self, record_type: type[object]) -> None:
        if record_type in self._building:
            return
        self._building.add(record_type)
        name = record_type.__name__
        self.definitions[name] = {}
        type_hints = get_type_hints(record_type)
        properties = {}
        for field in fields(record_type):
            schema = self.schema_for(type_hints[field.name])
            if field.name == "schema_epoch" and record_type in EPOCH_3_RECORDS:
                schema = {"const": 3}
            elif (
                record_type is Epoch3AnalysisFrameRevision
                and field.name == "identity_algorithm_version"
            ):
                schema = {"const": "measurement-identity.v1"}
            elif record_type is AnswerVersion and field.name == "status":
                schema = {"const": "provisional"}
            elif (
                field.name in SHA256_FIELDS
                or field.name.endswith("_sha256")
            ):
                schema = _with_string_pattern(schema, SHA256_PATTERN)
            elif field.name in SHA256_ARRAY_FIELDS:
                schema = {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "pattern": SHA256_PATTERN,
                    },
                    "minItems": 1,
                    "uniqueItems": True,
                }
            elif field.name in RECORD_SHA256_ARRAY_FIELDS.get(
                name,
                {},
            ):
                schema = {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "pattern": SHA256_PATTERN,
                    },
                }
                if field.name not in (
                    NON_UNIQUE_RECORD_SHA256_ARRAY_FIELDS.get(
                        name,
                        set(),
                    )
                ):
                    schema["uniqueItems"] = True
                if RECORD_SHA256_ARRAY_FIELDS[name][field.name]:
                    schema["minItems"] = 1
            if (
                field.name
                in {"revision_number", "version_number", "sequence"}
                and schema.get("type") == "integer"
            ):
                schema["minimum"] = 1
            if field.name == "accepted_head_version":
                schema["minimum"] = (
                    1 if record_type is QuestionRevision else 0
                )
            properties[field.name] = schema
        self.definitions[name] = {
            "type": "object",
            "additionalProperties": False,
            "required": [field.name for field in fields(record_type)],
            "properties": properties,
        }
        self._building.remove(record_type)


def _with_string_pattern(
    schema: dict[str, object],
    pattern: str,
) -> dict[str, object]:
    if schema.get("type") == "string":
        return {**schema, "pattern": pattern}
    if "anyOf" in schema:
        return {
            "anyOf": [
                (
                    {**candidate, "pattern": pattern}
                    if candidate.get("type") == "string"
                    else candidate
                )
                for candidate in schema["anyOf"]
            ]
        }
    raise TypeError("digest field must be a string or optional string")


def _document(
    *,
    schema_id: str,
    title: str,
    roots: tuple[type[object], ...],
) -> dict[str, object]:
    builder = SchemaBuilder()
    root_refs = [builder.ref(root) for root in roots]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "title": title,
        "oneOf": root_refs,
        "$defs": builder.definitions,
    }


def _actions_document() -> dict[str, object]:
    builder = SchemaBuilder()
    variants: list[dict[str, object]] = []
    for kind in ActionKind:
        payload_type = ACTION_PAYLOAD_TYPES[kind]
        variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "payload"],
                "properties": {
                    "kind": {"const": kind.value},
                    "payload": builder.ref(payload_type),
                },
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:waje-vnext:domain:actions:v3",
        "title": "WAJE vNext typed agent action proposal v3",
        "oneOf": variants,
        "$defs": builder.definitions,
    }


def _render(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    documents = {
        DOMAIN / "authority.v3.schema.json": _document(
            schema_id="urn:waje-vnext:domain:authority:v3",
            title="WAJE vNext epoch-3 authority and trust records",
            roots=(
                InvestigationCase,
                QuestionRevision,
                AnalysisFrameRevision,
                WorkPlanRevision,
                EvidenceRecord,
                AnswerVersion,
                MeasurementResolutionOutcome,
                ResolvedEvidenceObligation,
                EvidenceValidityRecord,
                ObligationSatisfactionRecord,
                SettlementPreconditionReport,
            ),
        ),
        DOMAIN / "actions.v3.schema.json": _actions_document(),
        DOMAIN / "context-packet.v3.schema.json": _document(
            schema_id="urn:waje-vnext:domain:context-packet:v3",
            title="WAJE vNext ContextPacket v3",
            roots=(ContextPacket,),
        ),
        DOMAIN / "runtime-amendment.v1.schema.json": _document(
            schema_id="urn:waje-vnext:domain:runtime-amendment:v1",
            title="WAJE vNext Gate 3 durable runtime amendment",
            roots=(
                MessageIngressRecord,
                PendingUserMessage,
                MessageBindingRequest,
                MessageImpactProposal,
                MessageImpactBinding,
                FrameCandidateRecord,
                ObjectionClosureRecord,
                FrameReviewRecord,
                FrameAdmissionProof,
                JobDispositionRecord,
                DispatcherRecoveryCursor,
                ModelConfigurationIdentity,
                ModelRequestArtifact,
                LogicalModelJob,
                ProviderAttemptRequest,
                ProviderAttemptReceipt,
                DurableModelResult,
                RunTraceManifest,
            ),
        ),
        DOMAIN / "planning.v1.schema.json": _document(
            schema_id="urn:waje-vnext:domain:planning:v1",
            title="WAJE vNext Gate 3.4 logical planning contracts",
            roots=(
                PlanAdoptionRecord,
                QueryBindingEnvelope,
                ConformanceExecutionSpec,
                LogicalExecutionAttempt,
            ),
        ),
        DOMAIN / "evidence.v1.schema.json": _document(
            schema_id="urn:waje-vnext:domain:evidence:v1",
            title="WAJE vNext Gate 3.5 evidence authority",
            roots=(
                CapabilityResultEnvelope,
                CapabilityResultReceipt,
                EvidenceRecord,
                EvidenceAdmissionRecord,
                EvidenceValidityRecord,
                EvidenceUseBinding,
                ObligationSatisfactionRecord,
            ),
        ),
        DOMAIN / "answering.v1.schema.json": _document(
            schema_id="urn:waje-vnext:domain:answering:v1",
            title="WAJE vNext Gate 3.5 provisional answer authority",
            roots=(
                AnalysisCheckDisposition,
                ProvisionalAnswerCandidate,
                ClaimPrecheckRecord,
                AnswerVersion,
                SettlementPreconditionReport,
            ),
        ),
        DOMAIN / "workflow.v1.schema.json": _document(
            schema_id="urn:waje-vnext:domain:workflow:v1",
            title="WAJE vNext Gate 3.5 workflow projection",
            roots=(
                WorkflowSnapshot,
                WorkflowApplicationReceipt,
                WorkflowProjectionHead,
                WorkflowReadModel,
            ),
        ),
    }
    check_only = "--check" in sys.argv
    stale: list[str] = []
    for path, document in documents.items():
        rendered = _render(document)
        if check_only:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(rendered, encoding="utf-8")
    if stale:
        raise SystemExit(
            "Generated contract schemas are stale:\n"
            + "\n".join(f"- {path}" for path in stale)
        )


if __name__ == "__main__":
    main()
