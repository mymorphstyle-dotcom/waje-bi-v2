from dataclasses import replace
from datetime import datetime
import unittest

from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.clickhouse_query_compiler import compile_clickhouse_query
from bi_agent.runtime.clickhouse_revenue_rows import (
    ClickHouseRevenueRows,
    _dataset_snapshots,
    trusted_active_dataset_snapshots,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    DatasetReleaseAuthorityRecord,
    DatasetSnapshot,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)

from tests.phase4.test_clickhouse_query_compiler import contract, dashboard_metric


class _Resolver:
    def __init__(self, record: DatasetReleaseAuthorityRecord):
        self.record = record

    def resolve_dataset_release(self, release_ref: str) -> DatasetReleaseAuthorityRecord:
        if release_ref != self.record.release_ref:
            raise KeyError(release_ref)
        return self.record


class DatasetReleaseAuthorityTest(unittest.TestCase):
    def test_authority_digest_covers_every_immutable_member_field(self):
        payloads = _release_payloads()
        baseline = build_dataset_release_authority_record(payloads)
        mutations = (
            ("watermark", "2026-06-01"),
            ("contract_ref", "contracts/sources/market-dashboard.source.yaml@drift"),
            ("permission_scopes", ["admin"]),
            ("loaded_at", "2026-06-03T01:00:00+00:00"),
        )

        for field, value in mutations:
            with self.subTest(field=field):
                drifted = ({**payloads[0], field: value}, payloads[1])
                changed = build_dataset_release_authority_record(drifted)
                self.assertNotEqual(
                    changed.authority_record_ref,
                    baseline.authority_record_ref,
                )

    def test_superseded_release_keeps_immutable_authority_but_is_not_visible(self):
        store = InMemoryConversationStore()
        old_payloads = _release_payloads(revision="dashboard-load:sha256:old")
        old_release = old_payloads[0]["release_ref"]
        store.publish_dataset_snapshot_release(
            release_ref=old_release,
            logical_snapshot_id="dashboard-logical",
            payloads=old_payloads,
        )
        new_payloads = _release_payloads(
            revision="dashboard-load:sha256:new",
            ref_suffix=":new",
        )
        store.publish_dataset_snapshot_release(
            release_ref=new_payloads[0]["release_ref"],
            logical_snapshot_id="dashboard-logical",
            payloads=new_payloads,
        )

        old_authority = store.resolve_dataset_release(old_release)
        self.assertEqual(old_authority.integrity_errors, ())
        typed = trusted_active_dataset_snapshots(store, purpose="context")
        catalog = DatasetCatalog(typed.values(), release_resolver=store)
        selected = catalog.resolve(
            "market_dashboard",
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+00:00"),
            permission_scope="analyst",
        )
        self.assertIn(":new", selected.snapshot_ref)

    def test_resolver_rejects_current_payload_drift_for_complete_projection(self):
        mutations = (
            ("watermark", "2026-06-01"),
            ("contract_ref", "contract:drift"),
            ("permission_scopes", ["admin"]),
            ("loaded_at", "2026-06-03T01:00:00+00:00"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                store = InMemoryConversationStore()
                payloads = _release_payloads()
                release_ref = payloads[0]["release_ref"]
                store.publish_dataset_snapshot_release(
                    release_ref=release_ref,
                    logical_snapshot_id="dashboard-logical",
                    payloads=payloads,
                )
                store.dataset_snapshots[payloads[0]["snapshot_ref"]][field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    "dataset_release_authority_record_mismatch",
                ):
                    store.resolve_dataset_release(release_ref)

    def test_authority_record_is_content_addressed_over_exact_member_facts(self):
        payloads = _release_payloads()
        record = build_dataset_release_authority_record(payloads)

        self.assertTrue(record.authority_record_ref.startswith("dataset-release-authority:sha256:"))
        self.assertEqual(record.digest, record.authority_record_ref.rsplit(":", 1)[-1])
        self.assertEqual(record.snapshot_refs, tuple(sorted(item["snapshot_ref"] for item in payloads)))
        self.assertEqual(record.dataset_ids, ("market_dashboard", "market_dashboard_channel"))
        self.assertEqual(record.integrity_errors, ())

        drifted = replace(
            record,
            member_projections=(
                replace(record.member_projections[0], physical_table="forged_table"),
                record.member_projections[1],
            ),
        )
        resolver = _Resolver(drifted)
        selected = _snapshot_from_payload(payloads[0], record.authority_record_ref)
        query = contract(
            dataset_id="market_dashboard",
            metrics=(dashboard_metric(),),
        )
        with self.assertRaisesRegex(ValueError, "dataset_release_authority_integrity"):
            compile_clickhouse_query(
                query,
                {selected.snapshot_ref: selected},
                release_resolver=resolver,
            )

    def test_compiler_requires_resolver_and_exact_member_match(self):
        payloads = _release_payloads()
        record = build_dataset_release_authority_record(payloads)
        selected = _snapshot_from_payload(payloads[0], record.authority_record_ref)
        query = contract(
            dataset_id="market_dashboard",
            metrics=(dashboard_metric(),),
        )

        with self.assertRaisesRegex(ValueError, "dataset_release_resolver_required"):
            compile_clickhouse_query(query, {selected.snapshot_ref: selected})

        with self.assertRaisesRegex(ValueError, "dataset_release_authority_member_mismatch"):
            compile_clickhouse_query(
                query,
                {
                    selected.snapshot_ref: replace(
                        selected,
                        rows_content_hash="f" * 64,
                    )
                },
                release_resolver=_Resolver(record),
            )

    def test_catalog_hides_staged_release_and_resolves_published_context(self):
        payloads = _release_payloads(channel_evidence="context_only")
        record = build_dataset_release_authority_record(payloads)
        snapshots = tuple(
            _snapshot_from_payload(payload, record.authority_record_ref)
            for payload in payloads
        )
        as_of = datetime.fromisoformat("2026-06-03T12:00:00+00:00")

        staged = DatasetCatalog(snapshots)
        with self.assertRaisesRegex(KeyError, "dataset_snapshot_unavailable"):
            staged.resolve(
                "market_dashboard_channel",
                as_of=as_of,
                permission_scope="analyst",
                evidence_states=("context_only",),
            )

        published = DatasetCatalog(snapshots, release_resolver=_Resolver(record))
        resolved = published.resolve(
            "market_dashboard_channel",
            as_of=as_of,
            permission_scope="analyst",
            evidence_states=("context_only",),
        )
        self.assertEqual(resolved.snapshot_ref, payloads[1]["snapshot_ref"])

    def test_untrusted_request_cannot_override_trusted_provider_snapshot(self):
        payloads = _release_payloads()
        record = build_dataset_release_authority_record(payloads)
        selected = _snapshot_from_payload(payloads[0], record.authority_record_ref)
        query = contract(
            dataset_id="market_dashboard",
            metrics=(dashboard_metric(),),
        )
        provider = ClickHouseRevenueRows(
            snapshots={selected.snapshot_ref: selected},
            release_resolver=_Resolver(record),
        )

        plan = provider.plan(
            {
                "run_id": "run-request-authority-forgery",
                "compiler_runtime_plan": {"query_contracts": (query,)},
                "dataset_snapshots": (
                    {
                        "snapshot_ref": selected.snapshot_ref,
                        "dataset_id": selected.dataset_id,
                        "release_ref": selected.release_ref,
                        "physical_table": "forged_table",
                        "rows_content_hash": "f" * 64,
                        "release_verified": True,
                    },
                ),
            },
            {},
            (),
        )

        self.assertIn("untrusted_dataset_snapshot_authority_fields", plan.reason)
        self.assertEqual(plan.snapshots, {})

    def test_single_save_uses_canonical_policy_even_when_marker_is_omitted(self):
        store = InMemoryConversationStore()
        payload = _release_payloads()[0]
        payload.pop("requires_release", None)

        with self.assertRaisesRegex(ValueError, "dataset_snapshot_release_required"):
            store.save_dataset_snapshot(payload)

    def test_in_memory_resolver_rejects_release_member_drift(self):
        store = InMemoryConversationStore()
        payloads = _release_payloads()
        release_ref = payloads[0]["release_ref"]
        store.publish_dataset_snapshot_release(
            release_ref=release_ref,
            logical_snapshot_id="dashboard-logical",
            payloads=payloads,
        )
        first = store.resolve_dataset_release(release_ref)
        self.assertEqual(first.integrity_errors, ())

        store.dataset_snapshots[payloads[0]["snapshot_ref"]]["physical_table"] = "drifted"
        with self.assertRaisesRegex(ValueError, "dataset_release_authority_record_mismatch"):
            store.resolve_dataset_release(release_ref)

    def test_in_memory_published_release_compiles_through_real_resolver(self):
        store = InMemoryConversationStore()
        payloads = _release_payloads()
        release_ref = payloads[0]["release_ref"]
        store.publish_dataset_snapshot_release(
            release_ref=release_ref,
            logical_snapshot_id="dashboard-logical",
            payloads=payloads,
        )
        typed = _dataset_snapshots(store.list_dataset_snapshots())
        selected = typed["snapshot:market_dashboard:1"]
        query = contract(
            dataset_id="market_dashboard",
            metrics=(dashboard_metric(),),
        )

        compiled = compile_clickhouse_query(
            query,
            {selected.snapshot_ref: selected},
            release_resolver=store,
        )

        self.assertIn(selected.physical_table, compiled.sql_text)
        self.assertEqual(
            compiled.parameters["physical_snapshot_id"],
            "dashboard-logical",
        )

    def test_in_memory_batch_rejects_reused_snapshot_ref_with_member_drift(self):
        store = InMemoryConversationStore()
        payloads = _release_payloads()
        release_ref = payloads[0]["release_ref"]
        store.publish_dataset_snapshot_release(
            release_ref=release_ref,
            logical_snapshot_id="dashboard-logical",
            payloads=payloads,
        )
        drifted = (
            {**payloads[0], "physical_table": "market_dashboard_daily__forged"},
            payloads[1],
        )

        with self.assertRaisesRegex(ValueError, "dataset_snapshot_published_immutable"):
            store.publish_dataset_snapshot_release(
                release_ref=release_ref,
                logical_snapshot_id="dashboard-logical",
                payloads=drifted,
            )

    def test_trusted_store_adapter_projects_only_active_purpose_eligible_snapshots(self):
        store = InMemoryConversationStore()
        old_payloads = _release_payloads(revision="dashboard-load:sha256:old")
        store.publish_dataset_snapshot_release(
            release_ref=old_payloads[0]["release_ref"],
            logical_snapshot_id="dashboard-logical",
            payloads=old_payloads,
        )
        new_payloads = _release_payloads(
            revision="dashboard-load:sha256:new",
            ref_suffix=":new",
        )
        store.publish_dataset_snapshot_release(
            release_ref=new_payloads[0]["release_ref"],
            logical_snapshot_id="dashboard-logical",
            payloads=new_payloads,
        )
        store.dataset_snapshots[old_payloads[0]["snapshot_ref"]][
            "historical_unknown_field"
        ] = "ignored-after-lifecycle-filter"

        claim_snapshots = trusted_active_dataset_snapshots(store, purpose="claim")
        context_snapshots = trusted_active_dataset_snapshots(store, purpose="context")

        self.assertEqual(
            set(claim_snapshots),
            {new_payloads[0]["snapshot_ref"]},
        )
        self.assertEqual(
            set(context_snapshots),
            {item["snapshot_ref"] for item in new_payloads},
        )


def _release_payloads(
    *,
    channel_evidence="context_only",
    revision="dashboard-load:sha256:reviewed",
    ref_suffix="",
):
    payloads = (
        _payload(f"snapshot:market_dashboard:1{ref_suffix}", "market_dashboard", "market_dashboard_daily__schema_overall", "a" * 64, "schema_overall", "claim_ready", "matched", revision=revision),
        _payload(f"snapshot:market_dashboard_channel:1{ref_suffix}", "market_dashboard_channel", "market_dashboard_channel_daily__schema_channel", "b" * 64, "schema_channel", channel_evidence, "mismatch", revision=revision),
    )
    release_ref = dataset_snapshot_release_ref(
        "dashboard-logical",
        revision,
        (item["snapshot_ref"] for item in payloads),
    )
    for payload in payloads:
        payload["release_ref"] = release_ref
    return payloads


def _payload(snapshot_ref, dataset_id, physical_table, rows_hash, schema, evidence, reconciliation, *, revision):
    return {
        "snapshot_ref": snapshot_ref,
        "snapshot_id": "dashboard-logical",
        "dataset_id": dataset_id,
        "physical_table": physical_table,
        "watermark": "2026-06-02",
        "schema_fingerprint": schema,
        "schema_fields": ["snapshot_id", "load_revision", "business_date", "paid_amount"],
        "contract_ref": "contracts/sources/market-dashboard.source.yaml@0.1",
        "permission_scopes": ["analyst"],
        "loaded_at": "2026-06-03T00:00:00+00:00",
        "status": "active",
        "evidence_state": evidence,
        "reconciliation_status": reconciliation,
        "reconciliation_ref": "reconciliation:dashboard",
        "logical_snapshot_id": "dashboard-logical",
        "load_revision": revision,
        "release_ref": "",
        "requires_release": True,
        "rows_content_hash": rows_hash,
    }


def _snapshot_from_payload(payload, authority_record_ref):
    return DatasetSnapshot(
        snapshot_ref=payload["snapshot_ref"],
        dataset_id=payload["dataset_id"],
        physical_table=payload["physical_table"],
        watermark=payload["watermark"],
        schema_fingerprint=payload["schema_fingerprint"],
        schema_fields=tuple(payload["schema_fields"]),
        contract_ref=payload["contract_ref"],
        permission_scopes=tuple(payload["permission_scopes"]),
        loaded_at=payload["loaded_at"],
        status=payload["status"],
        evidence_state=payload["evidence_state"],
        reconciliation_status=payload["reconciliation_status"],
        reconciliation_ref=payload["reconciliation_ref"],
        logical_snapshot_id=payload["logical_snapshot_id"],
        load_revision=payload["load_revision"],
        release_ref=payload["release_ref"],
        authority_record_ref=authority_record_ref,
        rows_content_hash=payload["rows_content_hash"],
        snapshot_id=payload.get("snapshot_id", ""),
        source_load_manifest_ref=payload.get("source_load_manifest_ref", ""),
        runtime_binding_ref=payload.get("runtime_binding_ref", ""),
        source_checksums=tuple(sorted((payload.get("source_checksums") or {}).items())),
        row_count=payload.get("row_count", -1),
        date_range=tuple(payload.get("date_range") or ()),
        no_data_partitions=tuple(payload.get("no_data_partitions") or ()),
        no_data_partition_windows=tuple(
            payload.get("no_data_partition_windows") or ()
        ),
    )


if __name__ == "__main__":
    unittest.main()
