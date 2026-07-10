from dataclasses import replace
import unittest

from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
)
from bi_agent.runtime.evidence_authority import (
    CompletenessRecord,
    RowsRecord,
    canonical_digest,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.analysis_asset_fixtures import verified_dimension_scan_asset


class AuthoritativeQueryChainTest(unittest.TestCase):
    def setUp(self):
        self.asset, self.context = verified_dimension_scan_asset(
            rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 10.0,
                    "amount": 10.0,
                    "channel": "A",
                },
            ),
            required_fields=("window_id", "amount", "channel"),
            resolved_windows={
                "target_day": {
                    "start_inclusive": "2026-06-02",
                    "end_exclusive": "2026-06-03",
                    "timezone": "Africa/Lagos",
                }
            },
        )
        self.resolver = self.context["evidence_resolver"]
        self.registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        self.binding = self.resolver.resolve_capability_binding(
            self.context["binding_manifest_ref"]
        )

    def test_valid_chain_recomputes_rows_and_completeness(self):
        chain = validate_authoritative_query_chain(
            self.binding,
            resolver=self.resolver,
            rows_loader=self.resolver.rows_loader,
            runtime_registry=self.registry,
        )

        self.assertEqual(chain.primary_results[0].row_count, 1)
        self.assertEqual(chain.primary_reports[0].analysis_readiness, "ready")

    def test_direct_payload_and_subclass_registries_cannot_authorize_chain(self):
        direct_payload_registry = RuntimeContractRegistry(self.registry._payload)

        class RegistrySubclass(RuntimeContractRegistry):
            pass

        subclass_registry = RegistrySubclass.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        for registry, expected in (
            (direct_payload_registry, "runtime_contract_registry_integrity"),
            (subclass_registry, "runtime_contract_registry_type_invalid"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(
                    AuthoritativeQueryChainError,
                    expected,
                ):
                    validate_authoritative_query_chain(
                        self.binding,
                        resolver=self.resolver,
                        rows_loader=self.resolver.rows_loader,
                        runtime_registry=registry,
                    )

    def test_redigested_dimension_chain_cannot_be_rebound_as_event_evidence(self):
        event = self.registry.capability_inputs("event_evidence")
        plan = dict(self.binding.plan_payload)
        plan.update(
            {
                "capability_id": "event_evidence",
                "minimum_readiness": event["minimum_readiness"],
                "degradation_policy": event["degradation_policy"],
                "supported_evidence_types": tuple(event["supported_evidence_types"]),
                "supported_claim_types": tuple(event["supported_claim_types"]),
                "maximum_claim_strength": event["maximum_claim_strength"],
                "capability_contract_signature": (
                    self.registry.capability_contract_signature("event_evidence")
                ),
                "maximum_claim_strength_rank": (
                    self.registry.maximum_claim_strength_rank(
                        event["maximum_claim_strength"]
                    )
                ),
            }
        )
        payload = dict(self.binding.binding_payload)
        payload.update(
            {
                "supported_evidence_types": tuple(event["supported_evidence_types"]),
                "supported_claim_types": tuple(event["supported_claim_types"]),
                "maximum_claim_strength": event["maximum_claim_strength"],
                "maximum_claim_strength_rank": (
                    self.registry.maximum_claim_strength_rank(
                        event["maximum_claim_strength"]
                    )
                ),
            }
        )
        forged = _resign_binding(
            self.binding,
            capability_id="event_evidence",
            capability_contract_signature=(
                self.registry.capability_contract_signature("event_evidence")
            ),
            supported_evidence_types=tuple(event["supported_evidence_types"]),
            supported_claim_types=tuple(event["supported_claim_types"]),
            maximum_claim_strength=event["maximum_claim_strength"],
            maximum_claim_strength_rank=self.registry.maximum_claim_strength_rank(
                event["maximum_claim_strength"]
            ),
            plan_payload=plan,
            binding_payload=payload,
        )

        with self.assertRaisesRegex(
            AuthoritativeQueryChainError,
            "capability_contract_slot",
        ):
            validate_authoritative_query_chain(
                forged,
                resolver=self.resolver,
                rows_loader=self.resolver.rows_loader,
                runtime_registry=self.registry,
            )

    def test_redigested_binding_cannot_expand_denormalized_claim_types(self):
        expanded = (*self.binding.supported_claim_types, "unreviewed_claim")
        payload = dict(self.binding.binding_payload)
        payload["supported_claim_types"] = expanded
        forged = _resign_binding(
            self.binding,
            supported_claim_types=expanded,
            binding_payload=payload,
        )

        with self.assertRaisesRegex(
            AuthoritativeQueryChainError,
            "capability_contract_binding_policy_mismatch",
        ):
            validate_authoritative_query_chain(
                forged,
                resolver=self.resolver,
                rows_loader=self.resolver.rows_loader,
                runtime_registry=self.registry,
            )

    def test_rows_ref_and_content_addressed_storage_ref_are_distinct(self):
        rows_record = self.resolver.resolve_rows_record(
            self.binding.rows_metadata_record_refs[0]
        )

        self.assertNotEqual(rows_record.rows_ref, rows_record.storage_ref)
        self.assertTrue(rows_record.storage_ref.startswith("rows-storage:sha256:"))
        self.assertIsNone(self.resolver.rows_loader.load_rows(rows_record.rows_ref))
        self.assertEqual(
            len(self.resolver.rows_loader.load_rows(rows_record.storage_ref)),
            rows_record.row_count,
        )

    def test_redigested_wrong_rows_count_and_unique_key_fail(self):
        original = self.resolver.resolve_rows_record(
            self.binding.rows_metadata_record_refs[0]
        )
        for field, value in (
            ("row_count", original.row_count + 1),
            ("unique_key_fields", ("window_id",)),
            ("storage_ref", f"rows-storage:sha256:{'0' * 64}"),
        ):
            with self.subTest(field=field):
                changed = replace(original, **{field: value})
                payload = {
                    "rows_ref": changed.rows_ref,
                    "rows_content_hash": changed.rows_content_hash,
                    "row_count": changed.row_count,
                    "unique_key_fields": changed.unique_key_fields,
                    "storage_ref": changed.storage_ref,
                }
                digest = canonical_digest(payload)
                changed = replace(
                    changed,
                    record_ref=f"rows-record:{changed.rows_ref}:{digest}",
                    record_digest=digest,
                    metadata_payload=payload,
                )

                class Resolver:
                    rows_loader = self.resolver.rows_loader

                    def __getattr__(_, name):
                        return getattr(self.resolver, name)

                    def resolve_rows_record(_, ref):
                        return changed if ref == changed.record_ref else self.resolver.resolve_rows_record(ref)

                forged_binding = _replace_binding_rows_record(
                    self.binding,
                    changed,
                )
                with self.assertRaises(AuthoritativeQueryChainError):
                    validate_authoritative_query_chain(
                        forged_binding,
                        resolver=Resolver(),
                        rows_loader=self.resolver.rows_loader,
                        runtime_registry=self.registry,
                    )

    def test_redigested_wrong_completeness_query_coverage_and_assertion_fail(self):
        original = self.resolver.resolve_completeness(
            self.binding.completeness_record_refs[0]
        )
        mutations = (
            {"query_contract_ref": "query:wrong"},
            {"coverage_summary": {**dict(original.report_payload["coverage_summary"]), "row_count": 2}},
            {"assertion_results": ({"assertion": "execution_succeeded", "passed": True},)},
        )
        for mutation in mutations:
            with self.subTest(mutation=tuple(mutation)):
                payload = {**dict(original.report_payload), **mutation}
                digest = canonical_digest(payload)
                changed = CompletenessRecord(
                    record_ref=f"completeness-record:{original.report_ref}:{digest}",
                    report_ref=original.report_ref,
                    query_contract_ref=str(payload["query_contract_ref"]),
                    result_ref=original.result_ref,
                    report_digest=digest,
                    report_payload=payload,
                )

                class Resolver:
                    rows_loader = self.resolver.rows_loader

                    def __getattr__(_, name):
                        return getattr(self.resolver, name)

                    def resolve_completeness(_, ref):
                        return changed if ref == changed.record_ref else self.resolver.resolve_completeness(ref)

                forged_binding = _replace_binding_completeness(
                    self.binding,
                    changed,
                )
                with self.assertRaises(AuthoritativeQueryChainError):
                    validate_authoritative_query_chain(
                        forged_binding,
                        resolver=Resolver(),
                        rows_loader=self.resolver.rows_loader,
                        runtime_registry=self.registry,
                    )


def _replace_binding_rows_record(binding, rows_record):
    payload = dict(binding.binding_payload)
    payload["rows_metadata_record_refs"] = (rows_record.record_ref,)
    payload["rows_metadata_record_digests"] = (rows_record.record_digest,)
    return _resign_binding(
        binding,
        rows_metadata_record_refs=(rows_record.record_ref,),
        rows_metadata_record_digests=(rows_record.record_digest,),
        binding_payload=payload,
    )


def _replace_binding_completeness(binding, record):
    payload = dict(binding.binding_payload)
    payload["completeness_record_refs"] = (record.record_ref,)
    payload["completeness_record_digests"] = (record.report_digest,)
    return _resign_binding(
        binding,
        completeness_record_refs=(record.record_ref,),
        completeness_record_digests=(record.report_digest,),
        binding_payload=payload,
    )


def _resign_binding(binding, **changes):
    changed = replace(binding, **changes)
    digest = canonical_digest(
        {
            "plan": changed.plan_payload,
            "binding": changed.binding_payload,
        }
    )
    return replace(
        changed,
        record_ref=f"capability-binding:{changed.capability_id}:{digest}",
        binding_digest=digest,
    )


if __name__ == "__main__":
    unittest.main()
