"""Durable two-stage admission for capability-native Evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from waje_vnext.domain.async_runtime import JobLease, OperationIdentity
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.evidence import (
    CapabilityResultEnvelope,
    CapabilityResultReceipt,
    EvidenceAdmissionProfile,
    EvidenceAdmissionRecord,
    EvidenceValidityRecord,
    ObligationSatisfactionRecord,
    build_capability_result_receipt,
    capability_result_receipt_payload_sha256,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationCompletionRecord,
    ObligationTerminalStatus,
)
from waje_vnext.storage.ports import AuthorityStore

from .obligation_runtime import DurableObligationCoordinator


@dataclass(frozen=True, slots=True)
class EvidenceAdmissionOutcome:
    """The complete T2 result committed under one transaction."""

    receipt: CapabilityResultReceipt
    admission: EvidenceAdmissionRecord
    validity: EvidenceValidityRecord
    satisfaction: ObligationSatisfactionRecord
    completion: ObligationCompletionRecord


class EvidenceRuntime:
    """Connect capability result landing to evidence admission.

    T1 persists an immutable capability result and receipt. T2 reloads that
    receipt, derives admission against current authority, and commits both the
    admission outcome and the obligation job disposition atomically.
    """

    def __init__(
        self,
        *,
        store: AuthorityStore,
        owner_id: str,
        profile: EvidenceAdmissionProfile,
        lease_duration: timedelta = timedelta(minutes=5),
        obligation_coordinator: DurableObligationCoordinator | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        if not isinstance(profile, EvidenceAdmissionProfile):
            raise TypeError("profile must be EvidenceAdmissionProfile")
        self._store = store
        self._profile = profile
        self._obligations = (
            obligation_coordinator
            if obligation_coordinator is not None
            else DurableObligationCoordinator(
                store=store,
                owner_id=owner_id,
                lease_duration=lease_duration,
            )
        )

    def land_result(
        self,
        *,
        envelope: CapabilityResultEnvelope,
        job_lease: JobLease,
        received_at: datetime,
    ) -> CapabilityResultReceipt:
        """Commit T1 without making the result usable by an Answer."""

        if job_lease.outbox_message_id != envelope.outbox_message_id:
            raise ValueError("result lease does not bind the capability outbox")
        message = self._store.get_outbox_message(
            envelope.outbox_message_id
        )
        operation_material = {
            "kind": "capability-result-receipt-operation.v1",
            "outbox_message_id": envelope.outbox_message_id,
            "capability_result_envelope_id": (
                envelope.capability_result_envelope_id
            ),
        }
        operation = OperationIdentity(
            operation_id=content_sha256(
                {**operation_material, "identity": "operation"}
            ),
            idempotency_key=content_sha256(
                {**operation_material, "identity": "idempotency"}
            ),
            causation_id=envelope.outbox_message_id,
            correlation_id=envelope.run_id,
            authority_revision=message.expected_authority_epoch,
            payload_sha256=(
                capability_result_receipt_payload_sha256(envelope)
            ),
        )
        receipt = build_capability_result_receipt(
            envelope=envelope,
            operation_identity=operation,
            delivery_owner_id=job_lease.owner_id,
            delivery_fencing_token=job_lease.fencing_token,
            received_at=received_at,
        )
        with self._store.atomic():
            self._store.assert_job_lease(
                job_lease,
                checked_at=received_at,
            )
            return self._store.land_capability_result(
                envelope=envelope,
                receipt=receipt,
                job_lease=job_lease,
                event_id=content_sha256(
                    {
                        "kind": "capability-result-landed-event.v1",
                        "receipt_id": receipt.capability_result_receipt_id,
                    }
                ),
                recorded_at=received_at,
            )

    def admit_result(
        self,
        *,
        receipt_id: str,
        admitted_at: datetime,
    ) -> EvidenceAdmissionOutcome:
        """Commit T2 and the obligation job terminal disposition together."""

        receipt = self._store.get_capability_result_receipt(receipt_id)
        with self._store.atomic():
            admission, validity, satisfaction = (
                self._store.admit_landed_result(
                    receipt_id=receipt.capability_result_receipt_id,
                    profile=self._profile,
                    event_id=content_sha256(
                        {
                            "kind": "evidence-admission-event.v1",
                            "receipt_id": (
                                receipt.capability_result_receipt_id
                            ),
                            "profile": self._profile.value,
                        }
                    ),
                    recorded_at=admitted_at,
                )
            )
            completion = self._obligations.admit_completion(
                schedule_id=receipt.schedule_id,
                obligation_id=admission.obligation_id,
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=(
                    receipt.capability_result_envelope_content_sha256
                ),
                completed_at=admitted_at,
            )
            return EvidenceAdmissionOutcome(
                receipt=receipt,
                admission=admission,
                validity=validity,
                satisfaction=satisfaction,
                completion=completion,
            )

    def recover_outbox(
        self,
        *,
        outbox_message_id: str,
        admitted_at: datetime,
    ) -> EvidenceAdmissionOutcome | None:
        """Resume T2 from the durable receipt left by an interrupted worker."""

        receipt = self._store.find_capability_result_receipt_by_outbox(
            outbox_message_id
        )
        if receipt is None:
            return None
        return self.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=admitted_at,
        )
