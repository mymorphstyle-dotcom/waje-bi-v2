from dataclasses import replace
from datetime import datetime
import unittest

from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.analysis_contracts import (
    QueryResultEnvelope,
)
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
)
from bi_agent.runtime.evidence_authority import (
    CompletenessRecord,
    RuntimeEvidenceAuthority,
    _record_completeness,
    _record_query_execution,
    canonical_digest,
    canonical_result_rows_hash,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    DatasetSnapshot,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.runtime.query_audit import query_audit_refs
from bi_agent.runtime.query_completeness import validate_query_result
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.authoritative_query_vectors import verified_dimension_scan_context
from tests.support.temporal_authority import resolved_test_daily_pair_authority


class AuthoritativeQueryChainTest(unittest.TestCase):
    def setUp(self):
        self.context = verified_dimension_scan_context(
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
            release_resolver=self.context["release_resolver"],
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
                "capability_id": "event_evidence",
                "capability_contract_signature": (
                    self.registry.capability_contract_signature("event_evidence")
                ),
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
            "capability_contract_plan_policy_mismatch",
        ):
            validate_authoritative_query_chain(
                forged,
                resolver=self.resolver,
                rows_loader=self.resolver.rows_loader,
                runtime_registry=self.registry,
                release_resolver=self.context["release_resolver"],
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
            "capability_contract_plan_identity_mismatch",
        ):
            validate_authoritative_query_chain(
                forged,
                resolver=self.resolver,
                rows_loader=self.resolver.rows_loader,
                runtime_registry=self.registry,
                release_resolver=self.context["release_resolver"],
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
                        return (
                            changed
                            if ref == changed.record_ref
                            else self.resolver.resolve_rows_record(ref)
                        )

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
            {
                "coverage_summary": {
                    **dict(original.report_payload["coverage_summary"]),
                    "row_count": 2,
                }
            },
            {
                "assertion_results": (
                    {"assertion": "execution_succeeded", "passed": True},
                )
            },
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
                        return (
                            changed
                            if ref == changed.record_ref
                            else self.resolver.resolve_completeness(ref)
                        )

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


class _DatasetReleaseAuthorityResolver:
    def __init__(self, record):
        self.record = record

    def resolve_dataset_release(self, release_ref):
        if release_ref != self.record.release_ref:
            raise KeyError(release_ref)
        return self.record


def _event_authority_context(registry):
    schema_fields = tuple(registry.dataset("external_event")["schema_fields"])
    snapshot = DatasetSnapshot(
        snapshot_ref="snapshot:external-event:authority-e2e",
        dataset_id="external_event",
        physical_table="business_events__a1a1a1a1a1a1a1a1",
        watermark="2026-06-08",
        schema_fingerprint="a1" * 32,
        schema_fields=schema_fields,
        contract_ref="contracts/sources/external-events.source.yaml@0.1",
        loaded_at="2026-06-03T00:00:00+00:00",
        status="active",
        evidence_state="context_only",
        reconciliation_status="not_applicable",
        logical_snapshot_id="external-events-authority-e2e",
        load_revision="external-events-load:sha256:authority-e2e",
        rows_content_hash="e" * 64,
        snapshot_id="external-events-authority-e2e",
        source_load_manifest_ref="load-manifest:event:authority-e2e",
        runtime_binding_ref="contracts/runtime/clickhouse-analysis-bindings.yaml@20",
        source_checksums=(("events.xlsx", "f" * 64),),
        row_count=1,
        date_range=("2026-05-01", "2026-06-08"),
    )
    release_ref = dataset_snapshot_release_ref(
        snapshot.logical_snapshot_id,
        snapshot.load_revision,
        (snapshot.snapshot_ref,),
    )
    snapshot = replace(snapshot, release_ref=release_ref)
    release_record = build_dataset_release_authority_record(
        ({**snapshot.to_dict(), "requires_release": True},)
    )
    snapshot = replace(
        snapshot,
        authority_record_ref=release_record.authority_record_ref,
    )
    release_resolver = _DatasetReleaseAuthorityResolver(release_record)
    outcome = compile_analysis_contract(
        run_id="run-event-authority-e2e",
        proposal={
            "requested_context_sources": ("external_event",),
            "claim_intents": ("candidate_mechanism",),
        },
        accepted_capabilities=("event_evidence",),
        catalog=DatasetCatalog((snapshot,), release_resolver=release_resolver),
        registry=registry,
        temporal_authority=resolved_test_daily_pair_authority(
            target="2026-06-02",
            baseline_id="previous_day",
        ),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        release_resolver=release_resolver,
    )
    contract = outcome.query_contracts[0]
    rows = tuple(
        {
            "window_id": window.window_id,
            "window_role": window.role,
            "observation_key": "event:holiday:reviewed",
            "event_count": 1,
            "source_family": "external_event",
            "event_id": "event:holiday:reviewed",
            "event_type": "holiday_context",
            "event_start_date": "2026-05-01",
            "event_end_date": "2026-06-08",
            "affected_scope": "Nigeria",
            "authority": "reviewed_workbook_pending_owner_review",
            "evidence_level": "context",
            "wording_limit": "context",
            "recurrence_kind": "",
            "recurrence_month_start": 0,
            "recurrence_day_start": 0,
            "recurrence_month_end": 0,
            "recurrence_day_end": 0,
            "payload": '{"description":"reviewed holiday"}',
        }
        for window in contract.resolved_windows
    )
    attempt_ref = "attempt:event-authority-e2e"
    authority = RuntimeEvidenceAuthority()
    result = ClickHouseQueryExecutor(
        _FaithfulRowsRuntime(rows),
        evidence_authority=authority,
        release_resolver=release_resolver,
    ).execute(
        contract,
        {snapshot.snapshot_ref: snapshot},
        execution_attempt_ref=attempt_ref,
    )
    report = validate_query_result(
        contract,
        result,
        snapshot,
        release_resolver=release_resolver,
    )
    _record_completeness(authority, report)
    return {
        "authority": authority,
        "release_resolver": release_resolver,
        "contract": contract,
        "result": result,
        "report": report,
        "plan": outcome.capability_plans[0],
        "snapshot": snapshot,
    }


class _FaithfulRowsRuntime:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def aggregate(self, sql, query_id, **kwargs):
        return self._result(sql, query_id, kwargs)

    def bounded_context(self, sql, query_id, **kwargs):
        return self._result(sql, query_id, kwargs)

    def _result(self, sql, query_id, kwargs):
        return ClickHouseQueryResult(
            ok=True,
            rows=self.rows,
            query_hash=canonical_digest(
                {"sql": sql, "parameters": kwargs.get("parameters", {})}
            ),
            query_id=query_id,
            provider_stats={
                "requested_settings": dict(kwargs.get("settings") or {}),
                "summary": {"read_rows": len(self.rows)},
            },
            execution_attempt_ref=kwargs.get("execution_attempt_ref", ""),
        )


def _dashboard_authority_context(registry):
    snapshot = DatasetSnapshot(
        snapshot_ref="snapshot:market-dashboard:release-e2e",
        dataset_id="market_dashboard",
        physical_table="market_dashboard_daily__schema1234567890",
        watermark="2026-06-02",
        schema_fingerprint="schema1234567890abcdef",
        schema_fields=(
            "snapshot_id",
            "load_revision",
            "business_date",
            "game",
            "paid_amount",
        ),
        contract_ref="contract:market-dashboard@1",
        loaded_at="2026-06-03T00:00:00+00:00",
        status="active",
        evidence_state="claim_ready",
        reconciliation_status="matched",
        reconciliation_ref="reconciliation:market-dashboard:matched",
        logical_snapshot_id="dashboard-logical",
        load_revision="dashboard-load:sha256:release-e2e",
        rows_content_hash="a" * 64,
        snapshot_id="dashboard-logical",
        source_load_manifest_ref="load-manifest:dashboard:release-e2e",
        runtime_binding_ref="runtime-binding:market-dashboard@1",
        source_checksums=(("market_dashboard.csv", "b" * 64),),
        row_count=1,
        date_range=("2026-06-02", "2026-06-02"),
    )
    channel_snapshot = replace(
        snapshot,
        snapshot_ref="snapshot:market-dashboard-channel:release-e2e",
        dataset_id="market_dashboard_channel",
        physical_table="market_dashboard_channel_daily__schema1234567890",
        schema_fields=(*snapshot.schema_fields, "channel"),
        evidence_state="context_only",
        reconciliation_status="mismatch",
        reconciliation_ref="reconciliation:market-dashboard-channel:mismatch",
        rows_content_hash="c" * 64,
        source_load_manifest_ref="load-manifest:dashboard-channel:release-e2e",
        runtime_binding_ref="runtime-binding:market-dashboard-channel@1",
        source_checksums=(("market_dashboard_channel.csv", "d" * 64),),
    )
    release_ref = dataset_snapshot_release_ref(
        snapshot.logical_snapshot_id,
        snapshot.load_revision,
        (snapshot.snapshot_ref, channel_snapshot.snapshot_ref),
    )
    snapshot = replace(snapshot, release_ref=release_ref)
    channel_snapshot = replace(channel_snapshot, release_ref=release_ref)
    release_record = build_dataset_release_authority_record(
        tuple(
            {**item.to_dict(), "requires_release": True}
            for item in (snapshot, channel_snapshot)
        )
    )
    snapshot = replace(
        snapshot,
        authority_record_ref=release_record.authority_record_ref,
    )
    release_resolver = _DatasetReleaseAuthorityResolver(release_record)
    catalog = DatasetCatalog(
        (snapshot,),
        release_resolver=release_resolver,
    )
    outcome = compile_analysis_contract(
        run_id="run-market-dashboard-release-e2e",
        proposal={
            "target_metrics": ("paid_amount",),
            "claim_intents": ("comparative_change",),
        },
        accepted_capabilities=("market_health_compare",),
        catalog=catalog,
        registry=registry,
        temporal_authority=resolved_test_daily_pair_authority(
            target="2026-06-02",
            baseline_id="previous_day",
        ),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        release_resolver=release_resolver,
    )
    contract = outcome.query_contracts[0]
    rows = tuple(
        {
            "window_id": window.window_id,
            "window_role": window.role,
            "observation_key": window.start_inclusive,
            "paid_amount": 100.0,
        }
        for window in contract.resolved_windows
    )
    query_hash = "hash:market-dashboard-release-e2e"
    attempt_ref = "attempt:market-dashboard-release-e2e"
    refs = query_audit_refs(
        query_hash,
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=contract.query_contract_id,
        execution_attempt_ref=attempt_ref,
        rows_content_hash=canonical_result_rows_hash(
            rows,
            contract.result_shape.unique_key,
        ),
    )
    result = QueryResultEnvelope(
        query_contract_ref=contract.query_contract_id,
        query_id="clickhouse:market-dashboard-release-e2e",
        query_hash=query_hash,
        result_ref=refs.result_ref,
        execution_status="succeeded",
        rows_ref=refs.rows_ref,
        row_count=len(rows),
        completeness_report_ref=refs.completeness_report_ref,
        rows=rows,
        observed_schema={
            field: "String" for field in contract.result_shape.required_fields
        },
        observed_windows=tuple(row["window_id"] for row in rows),
        observed_grain=contract.result_shape.grain,
        source_snapshot_refs=(snapshot.snapshot_ref,),
        execution_attempt_ref=attempt_ref,
    )
    report = validate_query_result(
        contract,
        result,
        snapshot,
        release_resolver=release_resolver,
    )
    authority = RuntimeEvidenceAuthority()
    _record_query_execution(
        authority,
        contract,
        result,
        {snapshot.snapshot_ref: snapshot},
    )
    _record_completeness(authority, report)
    return {
        "authority": authority,
        "release_resolver": release_resolver,
        "contract": contract,
        "result": result,
        "report": report,
        "plan": outcome.capability_plans[0],
    }


def _evidence_from_bound_dashboard_input(bound):
    return {
        "evidence_ref": "evidence:market-dashboard-release-e2e",
        "evidence_type": "statistical_association",
        "strength": "observed",
        "wording_limit": "supported",
        "limitations": (),
        "numeric_facts": {"paid_amount": 100.0},
        "typed_payload": {"paid_amount": 100.0},
        "capability_id": bound.capability_id,
        "analysis_contract_ref": bound.analysis_contract_ref,
        "capability_contract_ref": bound.capability_contract_ref,
        "query_contract_refs": (
            *bound.query_contract_refs,
            *bound.validation_query_contract_refs,
        ),
        "result_refs": (*bound.result_refs, *bound.validation_result_refs),
        "query_execution_record_refs": (
            *bound.query_execution_record_refs,
            *bound.validation_query_execution_record_refs,
        ),
        "query_execution_record_digests": (
            *bound.query_execution_record_digests,
            *bound.validation_query_execution_record_digests,
        ),
        "rows_metadata_record_refs": (
            *bound.rows_metadata_record_refs,
            *bound.validation_rows_metadata_record_refs,
        ),
        "rows_metadata_record_digests": (
            *bound.rows_metadata_record_digests,
            *bound.validation_rows_metadata_record_digests,
        ),
        "completeness_report_refs": (
            *bound.completeness_report_refs,
            *bound.validation_completeness_report_refs,
        ),
        "completeness_record_refs": (
            *bound.completeness_record_refs,
            *bound.validation_completeness_record_refs,
        ),
        "completeness_record_digests": (
            *bound.completeness_record_digests,
            *bound.validation_completeness_record_digests,
        ),
        "source_snapshot_refs": (
            *bound.source_snapshot_refs,
            *bound.validation_source_snapshot_refs,
        ),
        "supported_evidence_types": bound.supported_evidence_types,
        "supported_claim_types": bound.supported_claim_types,
        "maximum_claim_strength": bound.maximum_claim_strength,
        "maximum_claim_strength_rank": bound.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": bound.claim_strength_taxonomy_version,
        "input_status": bound.status,
        "input_completeness_statuses": bound.input_completeness_statuses,
        "binding_manifest_ref": bound.binding_manifest_ref,
        "binding_manifest_digest": bound.binding_manifest_digest,
    }


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
