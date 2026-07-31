"""Deterministic provider for controller and replay tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import datetime
import hashlib
import json
from pathlib import Path

from waje_vnext.domain.actions import AgentActionProposal
from waje_vnext.domain.controller import PrimaryAgentRequest
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.runtime_amendment import (
    FrameReviewDisposition,
    FrameReviewProposal,
    FrameReviewRequest,
    MessageBindingDisposition,
    MessageBindingRequest,
    MessageImpactKind,
    MessageImpactProposal,
    ModelConfigurationIdentity,
    ModelExecutionRole,
    ModelInputViewKind,
    ModelRequestArtifact,
    ProposedSemanticAssertion,
    SemanticAssertionKind,
)

from .base import PreparedModelInvocation, ProviderPermanentError


_FAKE_RELEASE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class ScriptedPrimaryAgentProvider:
    allows_test_role_multiplexing = True

    def __init__(self, proposals: Iterable[AgentActionProposal]) -> None:
        self._proposals = deque(proposals)
        self.requests: list[PrimaryAgentRequest] = []
        self.binding_requests: list[MessageBindingRequest] = []
        self.review_requests: list[FrameReviewRequest] = []

    def enqueue_proposals(
        self,
        *proposals: AgentActionProposal,
    ) -> None:
        self._proposals.extend(proposals)

    @property
    def provider_ref(self) -> str:
        return "waje-scripted-test-double"

    @property
    def model_ref(self) -> str:
        return "scripted-typed-result"

    @property
    def configuration_ref(self) -> str:
        return self.configuration_identity(
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        ).configuration_sha256

    def configuration_identity(
        self,
        execution_role: ModelExecutionRole,
    ) -> ModelConfigurationIdentity:
        return self._configuration(execution_role)

    def _configuration(
        self,
        execution_role: ModelExecutionRole,
    ) -> ModelConfigurationIdentity:
        return ModelConfigurationIdentity.build(
            execution_role=execution_role,
            provider_ref=self.provider_ref,
            endpoint_ref="python://in-process-test-double",
            protocol_ref="python-typed-test-double.v1",
            adapter_release_ref="waje-vnext://providers/scripted-test-double.v1",
            adapter_release_sha256=_FAKE_RELEASE_SHA256,
            model_ref=self.model_ref,
            thinking="disabled",
            stable_parameters={"deterministic_queue": True},
            delivery_policy_ref="waje-vnext://providers/no-retry-test-double.v1",
            max_attempts=1,
            timeout_seconds=None,
        )

    def describe_invocation(
        self,
        *,
        logical_model_job_id: str,
        logical_job_kind: str,
        request: object,
        typed_request_contract_ref: str,
        output_contract_ref: str,
        created_at: datetime,
    ) -> PreparedModelInvocation:
        if logical_job_kind == "primary_agent" and isinstance(
            request,
            PrimaryAgentRequest,
        ):
            execution_role = (
                ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
            )
            view_kind = ModelInputViewKind.AGENT_WORLD_VIEW
            view_ref = request.context_packet.packet_id
            view_sha256 = request.context_packet.content_sha256
        elif logical_job_kind == "message_binding" and isinstance(
            request,
            MessageBindingRequest,
        ):
            execution_role = (
                ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
            )
            view_kind = ModelInputViewKind.MESSAGE_BINDING_VIEW
            view_ref = request.message_id
            view_sha256 = content_sha256(request)
        elif logical_job_kind == "measurement_reviewer" and isinstance(
            request,
            FrameReviewRequest,
        ):
            execution_role = ModelExecutionRole.RUNTIME_REVIEWER
            view_kind = ModelInputViewKind.MEASUREMENT_REVIEW_VIEW
            view_ref = request.frame_candidate.frame_candidate_id
            view_sha256 = content_sha256(request)
        else:
            raise ProviderPermanentError(
                "scripted provider received an unsupported job kind"
            )
        request_body = {
            "protocol": "python-typed-test-double.v1",
            "typed_request": to_jsonable(request),
        }
        prompt_ref = "waje-vnext://test-prompts/{}.v1".format(
            logical_job_kind
        )
        tool_ref = "waje-vnext://test-tools/{}.v1".format(
            logical_job_kind
        )
        decoder_ref = "waje-vnext://test-decoders/{}.v1".format(
            logical_job_kind
        )
        artifact = ModelRequestArtifact(
            model_request_artifact_id=(
                "model-request:{}".format(logical_model_job_id)
            ),
            logical_model_job_id=logical_model_job_id,
            execution_role=execution_role,
            logical_job_kind=logical_job_kind,
            input_view_kind=view_kind,
            input_view_ref=view_ref,
            input_view_sha256=view_sha256,
            typed_request_contract_ref=typed_request_contract_ref,
            typed_request_sha256=content_sha256(request),
            prompt_bundle_ref=prompt_ref,
            prompt_bundle_sha256=content_sha256(
                {"prompt_ref": prompt_ref}
            ),
            tool_bundle_ref=tool_ref,
            tool_bundle_sha256=content_sha256({"tool_ref": tool_ref}),
            output_contract_ref=output_contract_ref,
            output_contract_sha256=content_sha256(
                {
                    "output_contract_ref": output_contract_ref,
                    "decoder_ref": decoder_ref,
                }
            ),
            decoder_release_ref=decoder_ref,
            decoder_release_sha256=_FAKE_RELEASE_SHA256,
            provider_request_body=request_body,
            provider_request_sha256=content_sha256(request_body),
            created_at=created_at,
        )
        return PreparedModelInvocation(
            configuration_identity=self._configuration(execution_role),
            request_artifact=artifact,
        )

    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal:
        self.requests.append(request)
        if not self._proposals:
            raise ProviderPermanentError("scripted provider has no proposal left")
        return self._proposals.popleft()

    def review(self, request: FrameReviewRequest) -> FrameReviewProposal:
        self.review_requests.append(request)
        return FrameReviewProposal(
            disposition=FrameReviewDisposition.ACCEPT,
            objections=(),
            review_summary="Independent test Reviewer accepted the candidate.",
        )

    def bind_message(
        self,
        request: MessageBindingRequest,
    ) -> MessageImpactProposal:
        self.binding_requests.append(request)
        return MessageImpactProposal(
            impact_kind=(
                MessageImpactKind.QUESTION_REVISION
                if request.prior_question_text is None
                else MessageImpactKind.FRAME_REVISION
            ),
            disposition=MessageBindingDisposition.ACCEPTED,
            assertions=(
                ProposedSemanticAssertion(
                    kind=SemanticAssertionKind.BUSINESS_CONSTRAINT,
                    value_json=json.dumps(
                        {"business_request": request.message_content},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    source_start_codepoint=0,
                    source_end_codepoint=len(request.message_content),
                    material=True,
                ),
            ),
            ambiguities=(),
            clarification_options=(),
            recommended_option_id=None,
        )
