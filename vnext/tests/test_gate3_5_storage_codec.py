from __future__ import annotations

import unittest

import test_gate3_5_answer_contracts as answer_fixtures
import test_gate3_5_evidence_contracts as evidence_fixtures
import test_gate3_5_workflow as workflow_fixtures
from gate1_fixtures import NOW
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.evidence import build_obligation_satisfaction
from waje_vnext.domain.runtime_amendment import DurableModelResult
from waje_vnext.domain.workflow import apply_workflow_fact
from waje_vnext.storage.codec import (
    decode_capability_result_envelope,
    decode_capability_result_receipt,
    decode_claim_precheck,
    decode_evidence_admission,
    decode_evidence_use_binding,
    decode_answer,
    decode_evidence,
    decode_evidence_validity,
    decode_obligation_satisfaction,
    decode_workflow_application_receipt,
    decode_workflow_snapshot,
    decode_durable_model_result,
    encode_record,
)


class Gate35StorageCodecTest(unittest.TestCase):
    def test_nested_frozen_json_round_trips_through_typed_decoder(
        self,
    ) -> None:
        payload = {
            "payload": {
                "measurement_design": {
                    "windows": [
                        {
                            "calendar_days": 7,
                            "observed_days": 6,
                        }
                    ]
                }
            }
        }
        record = DurableModelResult(
            durable_model_result_id="result:nested-json",
            logical_model_job_id="job:nested-json",
            provider_attempt_id="attempt:nested-json",
            result_kind="typed_agent_action",
            result_contract_ref="contract:nested-json",
            result_payload=payload,
            output_sha256=content_sha256(payload),
            recorded_at=NOW,
        )

        self.assertEqual(
            decode_durable_model_result(encode_record(record)),
            record,
        )

    def test_evidence_authority_round_trips_without_losing_typed_identity(
        self,
    ) -> None:
        fixture = evidence_fixtures.Gate35EvidenceContractsTest()
        fixture.setUp()
        satisfaction = build_obligation_satisfaction(
            obligation=fixture.obligation,
            admissions=(fixture.admission,),
            validities=(fixture.validity,),
            boundary_outcome=None,
            prior=None,
            recorded_at=NOW,
        )
        cases = (
            (fixture.evidence, decode_evidence),
            (fixture.envelope, decode_capability_result_envelope),
            (fixture.receipt, decode_capability_result_receipt),
            (fixture.admission, decode_evidence_admission),
            (fixture.validity, decode_evidence_validity),
            (fixture.use, decode_evidence_use_binding),
            (
                satisfaction,
                decode_obligation_satisfaction,
            ),
        )
        for record, decoder in cases:
            with self.subTest(record=type(record).__name__):
                decoded = decoder(encode_record(record))
                self.assertEqual(decoded, record)
                self.assertEqual(
                    content_sha256(decoded),
                    content_sha256(record),
                )

    def test_answer_authority_round_trips_without_losing_claim_links(
        self,
    ) -> None:
        fixture = answer_fixtures.Gate35AnswerContractsTest()
        fixture.setUp()
        bundle = fixture._compile()
        assert bundle.answer is not None
        for record, decoder in (
            (bundle.prechecks[0], decode_claim_precheck),
            (bundle.answer, decode_answer),
        ):
            with self.subTest(record=type(record).__name__):
                self.assertEqual(decoder(encode_record(record)), record)

    def test_workflow_snapshot_and_receipt_round_trip(self) -> None:
        model = workflow_fixtures.initial_workflow_read_model(
            workflow_fixtures.CASE_ID,
            realm=workflow_fixtures.REALM,
            evidence_profile=workflow_fixtures.EVIDENCE_PROFILE,
        )
        advanced = apply_workflow_fact(
            model,
            workflow_fixtures.plan_fact(),
        )
        self.assertEqual(
            decode_workflow_snapshot(encode_record(advanced.snapshot)),
            advanced.snapshot,
        )
        self.assertEqual(
            decode_workflow_application_receipt(
                encode_record(advanced.application_receipts[-1])
            ),
            advanced.application_receipts[-1],
        )


if __name__ == "__main__":
    unittest.main()
