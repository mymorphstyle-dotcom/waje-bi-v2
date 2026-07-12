import math
from dataclasses import asdict, replace
import unittest
import bi_agent.runtime.evidence_authority as evidence_authority_module

from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    query_contract_signature,
)
from bi_agent.runtime.answer_package import verify_answer_package
from bi_agent.runtime.capability_execution import (
    bind_capability_inputs,
    validate_bound_capability_input,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RuntimeEvidenceAuthority,
    _record_completeness,
    _record_capability_binding,
    _record_query_execution,
    canonical_digest,
    canonical_rows_hash,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.query_audit import query_audit_refs
from bi_agent.runtime.query_completeness import validate_query_result
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.test_clickhouse_query_compiler import metric as reviewed_metric
from tests.phase4.test_query_completeness import (
    _PAID_RELEASE_RESOLVER,
    baseline_contract,
    complete_rows,
    paid_snapshot,
    successful_result,
)


class RuntimeEvidenceAuthorityTest(unittest.TestCase):
    def test_query_execution_rejects_rehashed_noncanonical_contract_signature(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        result = successful_result(contract, rows=complete_rows())
        authority = RuntimeEvidenceAuthority()
        record = _record_query_execution(
            authority,
            contract,
            result,
            {snapshot.snapshot_ref: snapshot},
        )
        resigned_contract = replace(contract, contract_signature="f" * 64)
        refs = query_audit_refs(
            record.query_hash,
            resigned_contract.contract_signature,
            record.source_snapshot_refs,
            query_contract_ref=record.query_contract_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            rows_content_hash=record.rows_content_hash,
        )
        result_payload = {
            **dict(record.result_payload),
            "result_ref": refs.result_ref,
            "rows_ref": refs.rows_ref,
            "completeness_report_ref": refs.completeness_report_ref,
        }
        query_contract = asdict(resigned_contract)
        record_payload = {
            "query_contract": query_contract,
            "result": result_payload,
            "rows_content_hash": record.rows_content_hash,
            "source_snapshot_record_refs": list(record.source_snapshot_record_refs),
            "source_snapshot_record_digests": list(
                record.source_snapshot_record_digests
            ),
        }
        digest = canonical_digest(record_payload)
        resigned = replace(
            record,
            record_ref=f"query-execution:{refs.result_ref}:{digest}",
            record_digest=digest,
            record_payload=record_payload,
            contract_signature=resigned_contract.contract_signature,
            query_contract=query_contract,
            contract=resigned_contract,
            result_ref=refs.result_ref,
            rows_ref=refs.rows_ref,
            completeness_report_ref=refs.completeness_report_ref,
            result_payload=result_payload,
        )

        self.assertIn(
            "query_contract_signature_invalid",
            runtime_evidence_record_integrity_errors(resigned),
        )

    def test_binding_pins_immutable_completeness_record(self):
        contract = baseline_contract()
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        result = successful_result(contract, rows=complete_rows())
        authority = RuntimeEvidenceAuthority()
        query_record = _record_query_execution(
            authority,
            contract,
            result,
            {snapshot.snapshot_ref: snapshot},
        )
        self.assertNotIn("rows", query_record.result_payload)
        base = _record_completeness(
            authority,
            validate_query_result(contract, result, snapshot),
        )
        final = _record_completeness(
            authority,
            replace(
                validate_query_result(contract, result, snapshot),
                coverage_summary={"phase": "final"},
            ),
        )

        self.assertNotEqual(base.record_ref, final.record_ref)
        self.assertEqual(authority.resolve_completeness(base.record_ref), base)
        self.assertEqual(authority.resolve_completeness(final.record_ref), final)
        self.assertEqual(
            authority.resolve_latest_completeness(base.report_ref),
            final,
        )

    def test_production_binder_resolves_authoritative_rows_and_records_binding(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        capability = registry.capability_inputs("compare_periods")
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        result = successful_result(contract, rows=complete_rows())
        report = validate_query_result(contract, result, snapshot)
        authority = RuntimeEvidenceAuthority()
        query_record = _record_query_execution(
            authority,
            contract,
            result,
            {snapshot.snapshot_ref: snapshot},
        )
        plan = CapabilityExecutionPlan(
            capability_id="compare_periods",
            capability_contract_ref=registry.capability_contract_ref(
                "compare_periods"
            ),
            required_input_slots=(
                CapabilityInputSlot(
                    slot_id="daily_metric_baselines",
                    query_contract_refs=(contract.query_contract_id,),
                    required=True,
                    accepted_completeness=("complete",),
                    required_fields=contract.result_shape.required_fields,
                    required_window_ids=tuple(capability["required_windows"]),
                ),
            ),
            optional_input_slots=(),
            merge_strategy="by_query_family",
            minimum_readiness=capability["minimum_readiness"],
            degradation_policy=capability["degradation_policy"],
            supported_evidence_types=tuple(capability["supported_evidence_types"]),
            maximum_claim_strength=capability["maximum_claim_strength"],
            analysis_contract_ref=contract.analysis_contract_ref,
            supported_claim_types=tuple(capability["supported_claim_types"]),
            capability_contract_version=registry.contract_version,
            capability_contract_signature=registry.capability_contract_signature(
                "compare_periods"
            ),
            claim_strength_taxonomy_version=(
                registry.claim_strength_taxonomy_version
            ),
            maximum_claim_strength_rank=registry.maximum_claim_strength_rank(
                capability["maximum_claim_strength"]
            ),
        )
        caller_tampered = replace(
            result,
            rows=tuple(
                {**row, "paid_amount": 999999.0}
                for row in result.rows
            ),
        )

        bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: caller_tampered},
            reports={contract.query_contract_id: report},
            evidence_authority=authority,
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )

        self.assertEqual(bound.status, "ready", bound.reasons)
        self.assertNotEqual(
            bound.rows_by_slot["daily_metric_baselines"][0]["paid_amount"],
            999999.0,
        )
        self.assertTrue(bound.binding_manifest_ref)
        self.assertEqual(bound.query_execution_record_refs, (query_record.record_ref,))
        self.assertEqual(
            bound.query_execution_record_digests,
            (query_record.record_digest,),
        )
        rows_record = authority.resolve_rows(result.rows_ref)
        self.assertEqual(bound.rows_metadata_record_refs, (rows_record.record_ref,))
        self.assertEqual(
            bound.rows_metadata_record_digests,
            (rows_record.record_digest,),
        )
        binding = authority.resolve_capability_binding(bound.binding_manifest_ref)
        self.assertEqual(binding.binding_digest, bound.binding_manifest_digest)
        self.assertEqual(validate_bound_capability_input(bound, authority), "")
        evidence = {
            "evidence_ref": "compare:authoritative",
            "capability_id": "compare_periods",
            "analysis_contract_ref": bound.analysis_contract_ref,
            "capability_contract_ref": bound.capability_contract_ref,
            "query_contract_refs": bound.query_contract_refs,
            "query_execution_record_refs": bound.query_execution_record_refs,
            "query_execution_record_digests": bound.query_execution_record_digests,
            "rows_metadata_record_refs": bound.rows_metadata_record_refs,
            "rows_metadata_record_digests": bound.rows_metadata_record_digests,
            "result_refs": bound.result_refs,
            "completeness_report_refs": bound.completeness_report_refs,
            "completeness_record_refs": bound.completeness_record_refs,
            "completeness_record_digests": bound.completeness_record_digests,
            "source_snapshot_refs": bound.source_snapshot_refs,
            "supported_evidence_types": bound.supported_evidence_types,
            "supported_claim_types": bound.supported_claim_types,
            "maximum_claim_strength": bound.maximum_claim_strength,
            "maximum_claim_strength_rank": bound.maximum_claim_strength_rank,
            "claim_strength_taxonomy_version": (
                bound.claim_strength_taxonomy_version
            ),
            "input_status": bound.status,
            "input_completeness_statuses": bound.input_completeness_statuses,
            "binding_manifest_ref": bound.binding_manifest_ref,
            "binding_manifest_digest": bound.binding_manifest_digest,
            "evidence_type": "statistical_association",
            "wording_limit": "supported",
            "typed_payload": {},
            "limitations": (),
        }
        claim = {
            "text": "目标期付费金额发生变化。",
            "claim_strength": "observed",
            "claim_type": "comparative_change",
            "evidence_refs": ("compare:authoritative",),
        }
        self.assertEqual(
            verify_answer_package(
                draft_claims=(claim,),
                evidence=(evidence,),
                visible_limitations=(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                runtime_registry=registry,
                release_resolver=_PAID_RELEASE_RESOLVER,
            )["status"],
            "passed",
        )
        self.assertEqual(
            verify_answer_package(
                draft_claims=(claim,),
                evidence=(evidence,),
                visible_limitations=(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
            )["status"],
            "failed",
        )
        drifted_registry_contract = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        drifted_registry_contract._payload["contract_version"] = "drifted"
        drifted_registry = verify_answer_package(
            draft_claims=(claim,),
            evidence=(evidence,),
            visible_limitations=(),
            evidence_resolver=authority,
            rows_loader=authority.rows_loader,
            runtime_registry=drifted_registry_contract,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(drifted_registry["status"], "failed")
        self.assertIn(
            "runtime_contract_registry_integrity",
            next(
                item
                for item in drifted_registry["errors"]
                if item["code"] == "claim_missing_authoritative_provenance"
            )["missing"],
        )
        context_evidence = {
            "evidence_ref": "context:laundering",
            "evidence_type": "context_only",
            "wording_limit": "context_only",
            "typed_payload": {
                "paid_amount": 999999,
                "scope": "wrong_scope",
                "time_window": "wrong_window",
            },
            "limitations": ("context_only",),
        }
        mixed_claim = {
            **claim,
            "numbers": {"paid_amount": 999999},
            "scope": "wrong_scope",
            "time_window": "wrong_window",
            "evidence_refs": (
                "compare:authoritative",
                "context:laundering",
            ),
        }
        mixed = verify_answer_package(
            draft_claims=(mixed_claim,),
            evidence=(evidence, context_evidence),
            visible_limitations=("context_only",),
            evidence_resolver=authority,
            rows_loader=authority.rows_loader,
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(mixed["status"], "failed")
        self.assertIn(
            "context_evidence_in_publishable_refs",
            {error["code"] for error in mixed["errors"]},
        )
        self.assertIn(
            "number_mismatch",
            {error["code"] for error in mixed["errors"]},
        )
        separated_context = verify_answer_package(
            draft_claims=({
                **claim,
                "context_evidence_refs": ("context:laundering",),
            },),
            evidence=(evidence, context_evidence),
            visible_limitations=("context_only",),
            evidence_resolver=authority,
            rows_loader=authority.rows_loader,
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(separated_context["status"], "passed")

        for invalid_strength in ("strong", "invented_strength"):
            with self.subTest(invalid_strength=invalid_strength):
                strength_result = verify_answer_package(
                    draft_claims=({**claim, "claim_strength": invalid_strength},),
                    evidence=(evidence,),
                    visible_limitations=(),
                    evidence_resolver=authority,
                    rows_loader=authority.rows_loader,
                    runtime_registry=registry,
                    release_resolver=_PAID_RELEASE_RESOLVER,
                )
                self.assertEqual(strength_result["status"], "failed")
                self.assertTrue(
                    any(
                        error["code"]
                        in {
                            "claim_strength_exceeds_authority",
                            "claim_strength_unknown",
                        }
                        for error in strength_result["errors"]
                    )
                )
        for tampered_evidence, tampered_claim in (
            (
                {**evidence, "supported_claim_types": ("unreviewed_claim",)},
                claim,
            ),
            (
                {**evidence, "maximum_claim_strength": "unreviewed"},
                claim,
            ),
            (
                evidence,
                {**claim, "claim_type": "unreviewed_claim"},
            ),
        ):
            with self.subTest(tampered_claim=tampered_claim):
                self.assertEqual(
                    verify_answer_package(
                        draft_claims=(tampered_claim,),
                        evidence=(tampered_evidence,),
                        visible_limitations=(),
                        evidence_resolver=authority,
                        rows_loader=authority.rows_loader,
                        runtime_registry=registry,
                    )["status"],
                    "failed",
                )
        self.assertEqual(
            verify_answer_package(
                draft_claims=(claim,),
                evidence=(evidence,),
                visible_limitations=(),
            )["status"],
            "failed",
        )
        missing_binding = {**evidence, "binding_manifest_ref": "binding:missing"}
        missing_binding_verifier = verify_answer_package(
            draft_claims=(claim,),
            evidence=(missing_binding,),
            visible_limitations=(),
            evidence_resolver=authority,
            rows_loader=authority.rows_loader,
            runtime_registry=registry,
        )
        self.assertEqual(missing_binding_verifier["status"], "failed")
        missing_binding_error = next(
            error
            for error in missing_binding_verifier["errors"]
            if error["code"] == "claim_missing_authoritative_provenance"
        )
        self.assertIn(
            "capability_binding_record",
            missing_binding_error["missing"],
        )

        drifted_plan = replace(
            plan,
            supported_claim_types=("unreviewed_claim",),
        )
        drifted = bind_capability_inputs(
            drifted_plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_authority=authority,
            runtime_registry=registry,
        )
        self.assertEqual(drifted.status, "blocked")
        self.assertIn(
            "capability_contract_resolution_failed:"
            "capability_contract_plan_policy_mismatch",
            drifted.reasons,
        )

        class MetadataResolver:
            def resolve_query_execution(self, ref):
                return authority.resolve_query_execution(ref)

            def resolve_query_execution_record(self, ref):
                return authority.resolve_query_execution_record(ref)

            def resolve_rows(self, ref):
                return authority.resolve_rows(ref)

            def resolve_rows_record(self, ref):
                return authority.resolve_rows_record(ref)

            def resolve_snapshot(self, ref):
                return authority.resolve_snapshot(ref)

            def resolve_completeness(self, ref):
                return authority.resolve_completeness(ref)

            def resolve_capability_binding(self, ref):
                return authority.resolve_capability_binding(ref)

        malicious_registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        malicious_capability = malicious_registry._payload["capability_inputs"][
            "compare_periods"
        ]
        malicious_capability["supported_claim_types"] = [
            *malicious_capability["supported_claim_types"],
            "unreviewed_claim",
        ]
        malicious_plan = replace(
            plan,
            supported_claim_types=tuple(
                malicious_capability["supported_claim_types"]
            ),
            capability_contract_signature=(
                malicious_registry.capability_contract_signature("compare_periods")
            ),
        )
        malicious_binding_payload = dict(binding.binding_payload)
        malicious_binding_payload["supported_claim_types"] = (
            malicious_plan.supported_claim_types
        )
        malicious_binding_authority = RuntimeEvidenceAuthority()
        malicious_binding = _record_capability_binding(
            malicious_binding_authority,
            malicious_plan,
            malicious_binding_payload,
        )

        class SelfAuthorizingResolver(MetadataResolver):
            runtime_registry = malicious_registry

            def resolve_capability_binding(self, ref):
                if ref == malicious_binding.record_ref:
                    return malicious_binding
                return super().resolve_capability_binding(ref)

        self_authorized = verify_answer_package(
            draft_claims=({**claim, "claim_type": "unreviewed_claim"},),
            evidence=(
                {
                    **evidence,
                    "binding_manifest_ref": malicious_binding.record_ref,
                    "binding_manifest_digest": malicious_binding.binding_digest,
                    "supported_claim_types": malicious_plan.supported_claim_types,
                },
            ),
            visible_limitations=(),
            evidence_resolver=SelfAuthorizingResolver(),
            rows_loader=authority.rows_loader,
        )
        self.assertEqual(self_authorized["status"], "failed")
        self.assertIn(
            "runtime_contract_registry_type_invalid",
            next(
                item
                for item in self_authorized["errors"]
                if item["code"] == "claim_missing_authoritative_provenance"
            )["missing"],
        )
        explicitly_self_authorized = verify_answer_package(
            draft_claims=({**claim, "claim_type": "unreviewed_claim"},),
            evidence=(
                {
                    **evidence,
                    "binding_manifest_ref": malicious_binding.record_ref,
                    "binding_manifest_digest": malicious_binding.binding_digest,
                    "supported_claim_types": malicious_plan.supported_claim_types,
                },
            ),
            visible_limitations=(),
            evidence_resolver=SelfAuthorizingResolver(),
            rows_loader=authority.rows_loader,
            runtime_registry=malicious_registry,
        )
        self.assertEqual(explicitly_self_authorized["status"], "failed")
        self.assertIn(
            "runtime_contract_registry_integrity",
            next(
                item
                for item in explicitly_self_authorized["errors"]
                if item["code"] == "claim_missing_authoritative_provenance"
            )["missing"],
        )

        authority_writer = authority._runtime_writer()

        class ValidButWrongBindingWriter:
            def record_query_execution(self, contract, result, snapshots):
                return authority_writer.record_query_execution(
                    contract,
                    result,
                    snapshots,
                )

            def record_completeness(self, report):
                return authority_writer.record_completeness(report)

            def record_capability_binding(self, plan, binding_payload):
                return malicious_binding

        valid_but_wrong_writer_bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=MetadataResolver(),
            rows_loader=authority.rows_loader,
            evidence_writer=ValidButWrongBindingWriter(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(valid_but_wrong_writer_bound.status, "blocked")
        self.assertIn(
            "runtime_evidence_writer_record_invalid",
            valid_but_wrong_writer_bound.reasons[0],
        )

        class TamperedBindingResolver(MetadataResolver):
            def resolve_capability_binding(self, ref):
                record = authority.resolve_capability_binding(ref)
                return replace(
                    record,
                    supported_claim_types=("unreviewed_claim",),
                )

        tampered_binding_evidence = {
            **evidence,
            "supported_claim_types": ("unreviewed_claim",),
        }
        tampered_binding = verify_answer_package(
            draft_claims=({**claim, "claim_type": "unreviewed_claim"},),
            evidence=(tampered_binding_evidence,),
            visible_limitations=(),
            evidence_resolver=TamperedBindingResolver(),
            rows_loader=authority.rows_loader,
            runtime_registry=registry,
        )
        self.assertEqual(tampered_binding["status"], "failed")
        tampered_binding_error = next(
            error
            for error in tampered_binding["errors"]
            if error["code"] == "claim_missing_authoritative_provenance"
        )
        self.assertIn(
            "capability_binding_record_integrity",
            tampered_binding_error["missing"],
        )

        resigned_binding_payload = dict(binding.binding_payload)
        resigned_binding_payload["maximum_claim_strength_rank"] = (
            binding.maximum_claim_strength_rank + 1
        )
        resigned_binding_digest = canonical_digest(
            {
                "plan": binding.plan_payload,
                "binding": resigned_binding_payload,
            }
        )
        resigned_binding = replace(
            binding,
            record_ref=(
                f"capability-binding:{binding.capability_id}:"
                f"{resigned_binding_digest}"
            ),
            binding_digest=resigned_binding_digest,
            maximum_claim_strength_rank=binding.maximum_claim_strength_rank + 1,
            binding_payload=resigned_binding_payload,
        )

        class ResignedBindingResolver(MetadataResolver):
            def resolve_capability_binding(self, ref):
                if ref == resigned_binding.record_ref:
                    return resigned_binding
                return super().resolve_capability_binding(ref)

        resigned_binding_result = verify_answer_package(
            draft_claims=(claim,),
            evidence=(
                {
                    **evidence,
                    "binding_manifest_ref": resigned_binding.record_ref,
                    "binding_manifest_digest": resigned_binding.binding_digest,
                    "maximum_claim_strength_rank": (
                        resigned_binding.maximum_claim_strength_rank
                    ),
                },
            ),
            visible_limitations=(),
            evidence_resolver=ResignedBindingResolver(),
            rows_loader=authority.rows_loader,
            runtime_registry=registry,
        )
        resigned_binding_missing = next(
            item
            for item in resigned_binding_result["errors"]
            if item["code"] == "claim_missing_authoritative_provenance"
        )["missing"]
        self.assertIn(
            "maximum_claim_strength_rank_policy",
            resigned_binding_missing,
        )
        self.assertNotIn(
            "capability_binding_record_integrity",
            resigned_binding_missing,
        )

        class TamperedQueryResolver(MetadataResolver):
            def resolve_query_execution(self, ref):
                record = authority.resolve_query_execution(ref)
                return replace(record, row_count=record.row_count + 1)

        class WrongTypeResolver(MetadataResolver):
            def resolve_query_execution(self, ref):
                return object()

        class ResignedSnapshotResolver(MetadataResolver):
            def resolve_snapshot(self, ref):
                record = authority.resolve_snapshot(ref)
                changed = replace(record.snapshot, status="inactive")
                payload = asdict(changed)
                digest = canonical_digest(payload)
                return replace(
                    record,
                    record_ref=f"snapshot-record:{ref}:{digest}",
                    record_digest=digest,
                    payload=payload,
                    payload_digest=digest,
                    snapshot=changed,
                )

        class WrongTypeWriter:
            def record_query_execution(self, contract, result, snapshots):
                return object()

            def record_completeness(self, report):
                return object()

            def record_capability_binding(self, plan, binding_payload):
                return object()

        resigned_contract = replace(contract, permission_scope="admin")
        resigned_contract = replace(
            resigned_contract,
            contract_signature=query_contract_signature(resigned_contract),
        )
        resigned_refs = query_audit_refs(
            result.query_hash,
            resigned_contract.contract_signature,
            resigned_contract.dataset_snapshot_refs,
            query_contract_ref=resigned_contract.query_contract_id,
            execution_attempt_ref=result.execution_attempt_ref,
            rows_content_hash=query_record.rows_content_hash,
        )
        resigned_result = replace(
            result,
            result_ref=resigned_refs.result_ref,
            rows_ref=resigned_refs.rows_ref,
            completeness_report_ref=resigned_refs.completeness_report_ref,
        )
        resigned_snapshot = replace(
            snapshot,
            permission_scopes=("analyst",),
        )
        resigned_authority = RuntimeEvidenceAuthority()
        resigned_query_record = _record_query_execution(
            resigned_authority,
            resigned_contract,
            resigned_result,
            {resigned_snapshot.snapshot_ref: resigned_snapshot},
        )
        resigned_report = replace(
            report,
            report_ref=resigned_result.completeness_report_ref,
            result_ref=resigned_result.result_ref,
        )
        resigned_completeness = _record_completeness(
            resigned_authority,
            resigned_report,
        )
        resigned_rows_record = resigned_authority.resolve_rows(
            resigned_result.rows_ref
        )
        resigned_binding_payload = dict(binding.binding_payload)
        resigned_binding_payload.update(
            {
                "result_refs": (resigned_result.result_ref,),
                "query_execution_record_refs": (
                    resigned_query_record.record_ref,
                ),
                "query_execution_record_digests": (
                    resigned_query_record.record_digest,
                ),
                "rows_refs": (resigned_result.rows_ref,),
                "rows_metadata_record_refs": (
                    resigned_rows_record.record_ref,
                ),
                "rows_metadata_record_digests": (
                    resigned_rows_record.record_digest,
                ),
                "rows_content_hashes": (
                    resigned_query_record.rows_content_hash,
                ),
                "completeness_report_refs": (
                    resigned_report.report_ref,
                ),
                "completeness_record_refs": (
                    resigned_completeness.record_ref,
                ),
                "completeness_record_digests": (
                    resigned_completeness.report_digest,
                ),
            }
        )
        resigned_binding = _record_capability_binding(
            resigned_authority,
            plan,
            resigned_binding_payload,
        )
        resigned_evidence = {
            **evidence,
            "result_refs": (resigned_result.result_ref,),
            "query_execution_record_refs": (
                resigned_query_record.record_ref,
            ),
            "query_execution_record_digests": (
                resigned_query_record.record_digest,
            ),
            "rows_metadata_record_refs": (
                resigned_rows_record.record_ref,
            ),
            "rows_metadata_record_digests": (
                resigned_rows_record.record_digest,
            ),
            "completeness_report_refs": (resigned_report.report_ref,),
            "completeness_record_refs": (resigned_completeness.record_ref,),
            "completeness_record_digests": (
                resigned_completeness.report_digest,
            ),
            "binding_manifest_ref": resigned_binding.record_ref,
            "binding_manifest_digest": resigned_binding.binding_digest,
        }
        resigned_query_verifier = verify_answer_package(
            draft_claims=(claim,),
            evidence=(resigned_evidence,),
            visible_limitations=(),
            evidence_resolver=resigned_authority,
            rows_loader=resigned_authority.rows_loader,
            runtime_registry=registry,
        )
        self.assertEqual(resigned_query_verifier["status"], "failed")
        self.assertIn(
            "authoritative_query_chain_invalid:query_contract_runtime_policy",
            next(
                item
                for item in resigned_query_verifier["errors"]
                if item["code"] == "claim_missing_authoritative_provenance"
            )["missing"],
        )

        class ResignedPermissionResolver(MetadataResolver):
            def resolve_query_execution(self, ref):
                return resigned_authority.resolve_query_execution(
                    resigned_result.result_ref
                )

            def resolve_query_execution_record(self, ref):
                return resigned_authority.resolve_query_execution_record(ref)

            def resolve_rows(self, ref):
                return resigned_authority.resolve_rows(ref)

            def resolve_rows_record(self, ref):
                return resigned_authority.resolve_rows_record(ref)

            def resolve_snapshot(self, ref):
                return resigned_authority.resolve_snapshot(ref)

        protocol_bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=MetadataResolver(),
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(protocol_bound.status, "ready")

        tampered_query_bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=TamperedQueryResolver(),
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(tampered_query_bound.status, "blocked")
        self.assertIn("query_execution_record_integrity", tampered_query_bound.reasons[0])

        wrong_type_bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=WrongTypeResolver(),
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(wrong_type_bound.status, "blocked")
        self.assertIn("query_execution_record_integrity", wrong_type_bound.reasons[0])

        resigned_snapshot_bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=ResignedSnapshotResolver(),
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(resigned_snapshot_bound.status, "blocked")
        self.assertIn("snapshot_record_binding", resigned_snapshot_bound.reasons[0])

        resigned_permission_bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=ResignedPermissionResolver(),
            rows_loader=resigned_authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(resigned_permission_bound.status, "blocked")
        self.assertIn(
            "query_execution_ref_missing",
            resigned_permission_bound.reasons[0],
        )

        wrong_writer_bound = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=MetadataResolver(),
            rows_loader=authority.rows_loader,
            evidence_writer=WrongTypeWriter(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(wrong_writer_bound.status, "blocked")
        self.assertIn("runtime_evidence_writer_record_invalid", wrong_writer_bound.reasons[0])

        wrong_resolver = bind_capability_inputs(
            plan,
            results={contract.query_contract_id: result},
            reports={contract.query_contract_id: report},
            evidence_resolver=object(),
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            runtime_registry=registry,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        self.assertEqual(wrong_resolver.status, "blocked")
        self.assertTrue(
            wrong_resolver.reasons[0].startswith("runtime_evidence_resolution_failed:")
        )

    def test_public_api_is_read_only(self):
        authority = RuntimeEvidenceAuthority()

        self.assertFalse(hasattr(authority, "record_query_execution"))
        self.assertFalse(hasattr(authority, "record_completeness"))
        self.assertFalse(hasattr(authority, "record_capability_binding"))
        self.assertFalse(hasattr(authority, "_put"))
        self.assertFalse(hasattr(evidence_authority_module, "_WRITE_TOKEN"))
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "completeness_write_type_invalid",
        ):
            authority._runtime_writer().record_completeness(object())

    def test_rows_hash_is_unique_key_ordered(self):
        rows = (
            {"window_id": "target", "channel": "B", "amount": 2.0},
            {"window_id": "target", "channel": "A", "amount": 1.0},
        )

        self.assertEqual(
            canonical_rows_hash(rows, ("window_id", "channel")),
            canonical_rows_hash(tuple(reversed(rows)), ("window_id", "channel")),
        )

    def test_rows_hash_rejects_nan_and_non_scalar_unique_keys(self):
        cases = (
            ({"window_id": "target", "channel": "A", "amount": math.nan},),
            ({"window_id": "target", "channel": {"nested": "A"}, "amount": 1},),
        )

        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(EvidenceIntegrityError):
                    canonical_rows_hash(rows, ("window_id", "channel"))

    def test_same_authority_ref_cannot_point_to_different_rows(self):
        contract = baseline_contract()
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        result = successful_result(contract, rows=complete_rows())
        authority = RuntimeEvidenceAuthority()
        _record_query_execution(
            authority,
            contract,
            result,
            {snapshot.snapshot_ref: snapshot},
        )
        changed = replace(
            result,
            rows=tuple(
                {**row, "paid_amount": float(row["paid_amount"]) + 1.0}
                for row in result.rows
            ),
        )

        with self.assertRaises(EvidenceIntegrityError):
            _record_query_execution(
                authority,
                contract,
                changed,
                {snapshot.snapshot_ref: snapshot},
            )


if __name__ == "__main__":
    unittest.main()
