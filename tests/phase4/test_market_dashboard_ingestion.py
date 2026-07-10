from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import yaml

from tools.data.load_market_dashboard_clickhouse import (
    CHANNEL_TABLE,
    OVERALL_TABLE,
    DashboardLoadError,
    build_dataset_snapshot_payloads,
    load_market_dashboard_rows,
    persist_dataset_snapshot_payloads,
)
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.clickhouse_revenue_rows import _dataset_snapshots
from bi_agent.runtime.dataset_catalog import DatasetSnapshot


ROOT = Path(__file__).resolve().parents[2]
SOURCE_CONTRACT = ROOT / "contracts" / "sources" / "market-dashboard.source.yaml"
RUNTIME_BINDINGS = ROOT / "contracts" / "runtime" / "clickhouse-analysis-bindings.yaml"

FIELD_MAPPING = {
    "business_date": "日期",
    "game": "游戏",
    "active_users": "日活",
    "new_users": "新增",
    "revenue": "营收",
    "active_user_arpu": "日活arpu",
    "registrations": "注册人数",
    "new_devices": "新增设备",
    "login_accounts": "登录账户数",
    "registration_rate": "注册率",
    "zero_round_user_share": "0局用户占比",
    "gameplay_users": "游戏人数",
    "gameplay_rounds": "游戏局数",
    "app_avg_online_time": "app在线人均时长",
    "effective_user_app_avg_online_time": "有效用户app在线人均时长",
    "historical_paid_active_users": "日活历史付费人数",
    "first_paid_amount": "首充用户金额",
    "new_paid_amount": "新增付费金额",
    "login_user_avg_recharge": "登陆用户人均充值",
    "avg_first_paid_amount": "平均首充金额",
    "first_paid_rate": "首充率",
    "first_paid_users": "首充人数",
    "new_paid_rate": "新增付费率",
    "new_paid_users": "新增付费人数",
    "paid_users": "付费人数",
    "paid_amount": "付费金额",
    "recharge_channel_fee": "充值渠道手续费",
    "withdraw_request_amount": "申请提现金额",
    "withdraw_arrived_users": "提现到账人数",
    "withdraw_arrived_amount": "提现到账金额",
    "withdraw_to_recharge_ratio": "提充比",
    "withdraw_request_users": "申请提现人数",
    "withdraw_fee": "提现手续费",
    "withdraw_user_fee": "提现用户手续费",
    "aggregate_marketing_cost": "投放成本",
    "profit": "利润",
}
HEADERS = tuple(FIELD_MAPPING.values())


def source_row(date: str = "2026-06-02", **overrides: str) -> list[str]:
    values = {field: "1" for field in HEADERS}
    values.update({"日期": date, "游戏": "Waje Special", "付费金额": "3000"})
    values.update(overrides)
    return [values[field] for field in HEADERS]


class MarketDashboardIngestionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_csv(self, name: str, rows: list[list[str]]) -> Path:
        path = self.root / name
        path.write_text(
            "\n".join(",".join(value for value in row) for row in rows) + "\n",
            encoding="utf-8-sig",
        )
        return path

    def overall_fixture(self, *, paid_amount: str = "3000") -> Path:
        return self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv",
            [list(HEADERS), source_row(**{"付费金额": paid_amount})],
        )

    def test_parses_utf8_sig_overall_and_filename_channel_rows(self):
        channel = self.write_csv(
            "WajeSpecial_2024-01-01_2026-06-02.csv",
            [list(HEADERS), source_row(**{"付费金额": "2500", "投放成本": "100"})],
        )

        rows, manifest = load_market_dashboard_rows(
            self.overall_fixture(), (channel,), snapshot_id="dashboard-20260602"
        )

        self.assertEqual(rows.channel_rows[0]["channel"], "WajeSpecial")
        self.assertEqual(rows.overall_rows[0]["paid_amount"], 3000.0)
        self.assertEqual(rows.overall_rows[0]["snapshot_id"], "dashboard-20260602")
        self.assertEqual(manifest.watermark, "2026-06-02")

    def test_empty_channel_file_is_no_data_not_zero_observation(self):
        empty = self.write_csv(
            "Empty_2024-01-01_2026-06-02.csv", [list(HEADERS)]
        )

        rows, manifest = load_market_dashboard_rows(
            self.overall_fixture(), (empty,), snapshot_id="s1"
        )

        self.assertEqual(rows.channel_rows, ())
        self.assertIn("Empty", manifest.no_data_partitions)
        self.assertEqual(manifest.channel_row_count, 0)
        self.assertEqual(
            manifest.no_data_partition_windows,
            ("Empty@2024-01-01:2026-06-02",),
        )
        self.assertEqual(manifest.reconciliation.status, "not_comparable")
        channel_snapshot = build_dataset_snapshot_payloads(manifest)[1]
        self.assertEqual(channel_snapshot["status"], "no_data")
        self.assertEqual(channel_snapshot["watermark"], "2026-06-02")
        self.assertEqual(
            channel_snapshot["schema_fields"][:5],
            ["snapshot_id", "load_revision", "business_date", "game", "channel"],
        )

    def test_missing_tail_cells_are_structural_errors_not_nullable_values(self):
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv",
            [list(HEADERS), source_row()[:-1]],
        )

        with self.assertRaisesRegex(DashboardLoadError, "source_row_missing_cells"):
            load_market_dashboard_rows(overall, (), snapshot_id="s1")

    def test_headers_must_exactly_match_the_reviewed_source_contract(self):
        wrong_headers = [field for field in HEADERS if field != "付费金额"] + ["金额"]
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv", [wrong_headers, source_row()]
        )

        with self.assertRaisesRegex(
            DashboardLoadError,
            r"source_header_mismatch:.*missing=付费金额.*unexpected=金额",
        ):
            load_market_dashboard_rows(overall, (), snapshot_id="s1")

    def test_channel_is_derived_only_from_reviewed_trailing_date_filename(self):
        unreviewed = self.write_csv(
            "WajeSpecial-2024-01-01-2026-06-02.csv",
            [list(HEADERS), source_row()],
        )

        with self.assertRaisesRegex(
            DashboardLoadError, "channel_filename_contract_mismatch"
        ):
            load_market_dashboard_rows(
                self.overall_fixture(), (unreviewed,), snapshot_id="s1"
            )

    def test_blank_and_null_numeric_values_are_nullable(self):
        row = source_row(**{"付费金额": "", "日活arpu": "NULL"})
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv", [list(HEADERS), row]
        )

        rows, _ = load_market_dashboard_rows(overall, (), snapshot_id="s1")

        self.assertIsNone(rows.overall_rows[0]["paid_amount"])
        self.assertIsNone(rows.overall_rows[0]["active_user_arpu"])

    def test_nan_numeric_marker_is_nullable_and_never_inserted_as_non_finite(self):
        row = source_row(**{"日活arpu": "nan"})
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv", [list(HEADERS), row]
        )

        rows, _ = load_market_dashboard_rows(overall, (), snapshot_id="s1")

        self.assertIsNone(rows.overall_rows[0]["active_user_arpu"])

    def test_nan_is_rejected_when_field_contract_does_not_declare_it_missing(self):
        row = source_row(**{"付费金额": "nan"})
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv", [list(HEADERS), row]
        )

        with self.assertRaisesRegex(
            DashboardLoadError,
            r"invalid_numeric_value:.*:row=2:field=付费金额",
        ):
            load_market_dashboard_rows(overall, (), snapshot_id="s1")

    def test_extra_csv_cells_are_rejected_as_malformed_row(self):
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv",
            [list(HEADERS), [*source_row(), "unexpected"]],
        )

        with self.assertRaisesRegex(DashboardLoadError, "source_row_extra_cells"):
            load_market_dashboard_rows(overall, (), snapshot_id="s1")

    def test_grain_keys_and_reviewed_game_scope_are_enforced(self):
        for game, reason in (("", "grain_key_empty:game"), ("Other Game", "game_scope_mismatch")):
            with self.subTest(game=game):
                overall = self.write_csv(
                    "大盘_2024-01-01_2026-06-02.csv",
                    [list(HEADERS), source_row(**{"游戏": game})],
                )
                with self.assertRaisesRegex(DashboardLoadError, reason):
                    load_market_dashboard_rows(overall, (), snapshot_id="s1")

    def test_reversed_empty_filename_window_is_rejected(self):
        channel = self.write_csv(
            "A_2026-06-02_2024-01-01.csv", [list(HEADERS)]
        )

        with self.assertRaisesRegex(DashboardLoadError, "filename_date_range_invalid"):
            load_market_dashboard_rows(
                self.overall_fixture(), (channel,), snapshot_id="s1"
            )

    def test_numeric_values_are_decimal_and_quantized_by_field_contract(self):
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv",
            [
                list(HEADERS),
                source_row(
                    **{
                        "日活": "100",
                        "付费金额": "3000.1234567890129",
                        "注册率": "0.1234567890123456789",
                    }
                ),
            ],
        )

        rows, _ = load_market_dashboard_rows(overall, (), snapshot_id="s1")

        self.assertEqual(rows.overall_rows[0]["active_users"], Decimal("100"))
        self.assertEqual(rows.overall_rows[0]["paid_amount"], Decimal("3000.123456789013"))
        self.assertEqual(
            rows.overall_rows[0]["registration_rate"],
            Decimal("0.123456789012345679"),
        )

    def test_every_field_declares_rounding_loss_and_canonicalization_policy(self):
        source = yaml.safe_load(SOURCE_CONTRACT.read_text(encoding="utf-8"))

        for field, contract in source["field_contracts"].items():
            with self.subTest(field=field):
                self.assertIn("rounding_mode", contract)
                self.assertIn("loss_policy", contract)
                self.assertEqual(
                    contract["canonicalization_version"],
                    source["runtime_binding"]["canonicalization_version"],
                )

    def test_invalid_numeric_value_fails_with_file_row_and_field(self):
        row = source_row(**{"付费金额": "three thousand"})
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv", [list(HEADERS), row]
        )

        with self.assertRaisesRegex(
            DashboardLoadError,
            r"invalid_numeric_value:.*:row=2:field=付费金额",
        ):
            load_market_dashboard_rows(overall, (), snapshot_id="s1")

    def test_manifest_and_snapshot_payloads_are_content_addressed(self):
        rows, manifest = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s1"
        )

        payloads = build_dataset_snapshot_payloads(manifest)

        self.assertRegex(manifest.manifest_ref, r"^source-load-manifest:sha256:[0-9a-f]{64}$")
        self.assertEqual(manifest.snapshot_ref, payloads[0]["snapshot_ref"])
        self.assertRegex(payloads[0]["snapshot_ref"], r"^dataset-snapshot:sha256:[0-9a-f]{64}$")
        self.assertEqual(payloads[0]["dataset_id"], "market_dashboard")
        self.assertEqual(payloads[1]["dataset_id"], "market_dashboard_channel")
        self.assertEqual(payloads[0]["status"], "active")
        self.assertEqual(payloads[0]["source_load_manifest_ref"], manifest.manifest_ref)
        self.assertRegex(manifest.overall_rows_content_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(payloads[0]["rows_content_hash"], manifest.overall_rows_content_hash)
        self.assertEqual(rows.overall_rows[0]["snapshot_id"], "s1")
        self.assertNotEqual(manifest.physical_table, OVERALL_TABLE)
        self.assertTrue(manifest.physical_table.startswith(f"{OVERALL_TABLE}__"))
        self.assertTrue(
            manifest.channel_physical_table.startswith(f"{CHANNEL_TABLE}__")
        )

    def test_loader_payload_roundtrips_through_strict_typed_snapshot_boundary(self):
        self.assertTrue(
            {"release_ref", "authority_record_ref", "rows_content_hash"}.issubset(
                DatasetSnapshot.__dataclass_fields__
            )
        )
        _, manifest = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s1"
        )
        payload = {
            **build_dataset_snapshot_payloads(manifest)[0],
            "authority_record_ref": "dataset-release-authority:sha256:" + "a" * 64,
        }

        snapshots = _dataset_snapshots((payload,))

        snapshot = snapshots[payload["snapshot_ref"]]
        self.assertEqual(snapshot.release_ref, payload["release_ref"])
        self.assertEqual(snapshot.authority_record_ref, payload["authority_record_ref"])
        self.assertEqual(snapshot.rows_content_hash, payload["rows_content_hash"])
        with self.assertRaisesRegex(ValueError, "unexpected:unknown_authority"):
            _dataset_snapshots(({**payload, "unknown_authority": True},))

    def test_release_preflight_rejects_incomplete_or_inconsistent_batches_before_db(self):
        _, manifest = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s1"
        )
        overall, channel = build_dataset_snapshot_payloads(manifest)

        class NoDatabaseCalls:
            def __init__(self):
                self.calls = 0

            def list_dataset_snapshots(self, dataset_id=""):
                self.calls += 1
                return ()

            def publish_dataset_snapshot_release(self, **kwargs):
                self.calls += 1

        cases = (
            (overall,),
            (overall, overall),
            (overall, {**channel, "logical_snapshot_id": "other"}),
            (overall, {**channel, "load_revision": "other"}),
            (overall, {**channel, "release_ref": "dataset-release:sha256:" + "0" * 64}),
        )
        for payloads in cases:
            with self.subTest(payloads=payloads):
                store = NoDatabaseCalls()
                with self.assertRaisesRegex(DashboardLoadError, "postgres_release_preflight"):
                    persist_dataset_snapshot_payloads(store, payloads)
                self.assertEqual(store.calls, 0)

    def test_same_sources_produce_same_manifest_and_changed_source_changes_it(self):
        overall = self.overall_fixture()
        _, first = load_market_dashboard_rows(overall, (), snapshot_id="s1")
        _, second = load_market_dashboard_rows(overall, (), snapshot_id="s1")
        changed = self.overall_fixture(paid_amount="3001")
        _, third = load_market_dashboard_rows(changed, (), snapshot_id="s1")

        self.assertEqual(asdict(first), asdict(second))
        self.assertNotEqual(first.manifest_ref, third.manifest_ref)
        self.assertNotEqual(first.source_checksums, third.source_checksums)

    def test_schema_policy_change_creates_new_physical_load_revision(self):
        contract = yaml.safe_load(SOURCE_CONTRACT.read_text(encoding="utf-8"))
        contract["field_contracts"]["paid_amount"]["loss_policy"] = "reject_invalid"
        changed_contract = self.root / "changed-market-dashboard.source.yaml"
        changed_contract.write_text(
            yaml.safe_dump(contract, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        _, original = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s1"
        )
        _, changed = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s1",
            source_contract_path=changed_contract,
        )

        self.assertNotEqual(original.schema_fingerprint, changed.schema_fingerprint)
        self.assertNotEqual(original.load_revision, changed.load_revision)
        self.assertNotEqual(original.physical_table, changed.physical_table)

    def test_replacing_same_snapshot_id_supersedes_prior_active_postgres_ref(self):
        store = InMemoryConversationStore()
        overall = self.overall_fixture()
        _, first_manifest = load_market_dashboard_rows(overall, (), snapshot_id="s1")
        first_payloads = build_dataset_snapshot_payloads(first_manifest)
        persist_dataset_snapshot_payloads(store, first_payloads)
        changed = self.overall_fixture(paid_amount="3001")
        _, second_manifest = load_market_dashboard_rows(changed, (), snapshot_id="s1")
        second_payloads = build_dataset_snapshot_payloads(second_manifest)

        result = persist_dataset_snapshot_payloads(store, second_payloads)

        overall_snapshots = store.list_dataset_snapshots("market_dashboard")
        self.assertEqual(len(overall_snapshots), 2)
        self.assertEqual(overall_snapshots[0]["status"], "superseded")
        self.assertEqual(
            overall_snapshots[0]["superseded_by_release"],
            second_payloads[0]["release_ref"],
        )
        self.assertEqual(overall_snapshots[1]["status"], "active")
        self.assertIn(first_payloads[0]["snapshot_ref"], result.superseded_refs)
        self.assertEqual(result.active_refs, tuple(item["snapshot_ref"] for item in second_payloads))

    def test_persistence_result_exposes_only_release_join_verified_payloads(self):
        store = InMemoryConversationStore()
        _, manifest = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s1"
        )

        result = persist_dataset_snapshot_payloads(
            store,
            build_dataset_snapshot_payloads(manifest),
        )

        self.assertEqual(len(result.verified_payloads), 2)
        self.assertTrue(
            all(
                item["authority_record_ref"]
                == result.authority_record["authority_record_ref"]
                for item in result.verified_payloads
            )
        )
        self.assertEqual(
            result.authority_record["snapshot_refs"],
            tuple(sorted(result.active_refs)),
        )
        typed = _dataset_snapshots(result.verified_payloads)
        self.assertEqual(set(typed), set(result.active_refs))

    def test_persisting_snapshot_keeps_other_active_snapshot_ids_versioned(self):
        store = InMemoryConversationStore()
        _, historical_manifest = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s0"
        )
        persist_dataset_snapshot_payloads(
            store,
            build_dataset_snapshot_payloads(historical_manifest),
        )
        _, manifest = load_market_dashboard_rows(
            self.overall_fixture(), (), snapshot_id="s1"
        )
        payloads = build_dataset_snapshot_payloads(manifest)

        persist_dataset_snapshot_payloads(store, payloads)

        active = [
            item
            for item in store.list_dataset_snapshots("market_dashboard")
            if item["status"] == "active"
        ]
        self.assertEqual({item["snapshot_id"] for item in active}, {"s0", "s1"})

    def test_paid_amount_reconciliation_is_typed_and_date_scoped(self):
        first = self.write_csv(
            "A_2024-01-01_2026-06-02.csv",
            [list(HEADERS), source_row(**{"付费金额": "1000"})],
        )
        second = self.write_csv(
            "B_2024-01-01_2026-06-02.csv",
            [list(HEADERS), source_row(**{"付费金额": "2000"})],
        )

        _, matched = load_market_dashboard_rows(
            self.overall_fixture(), (first, second), snapshot_id="s1"
        )
        _, mismatch = load_market_dashboard_rows(
            self.overall_fixture(paid_amount="3001"), (first, second), snapshot_id="s1"
        )

        self.assertEqual(matched.reconciliation.status, "matched")
        self.assertEqual(matched.reconciliation.reasons, ())
        self.assertEqual(mismatch.reconciliation.status, "mismatch")
        self.assertIn("paid_amount_mismatch:2026-06-02", mismatch.reconciliation.reasons)
        self.assertEqual(mismatch.reconciliation.compared_dates, ("2026-06-02",))

    def test_complementary_duplicate_channel_rows_are_aggregated_to_contract_grain(self):
        first = source_row(
            **{"新增设备": "13", "注册率": "1", "投放成本": "0", "付费金额": "0"}
        )
        second = source_row(
            **{"新增设备": "0", "注册率": "0", "投放成本": "10", "付费金额": "0"}
        )
        channel = self.write_csv(
            "A_2024-01-01_2026-06-02.csv", [list(HEADERS), first, second]
        )

        rows, manifest = load_market_dashboard_rows(
            self.overall_fixture(paid_amount="0"), (channel,), snapshot_id="s1"
        )

        self.assertEqual(len(rows.channel_rows), 1)
        self.assertEqual(rows.channel_rows[0]["new_devices"], 13.0)
        self.assertEqual(rows.channel_rows[0]["aggregate_marketing_cost"], 10.0)
        self.assertEqual(rows.channel_rows[0]["registration_rate"], 1.0)
        self.assertEqual(manifest.channel_source_row_count, 2)
        self.assertEqual(manifest.channel_row_count, 1)

    def test_duplicate_non_additive_values_with_multiple_signals_are_rejected(self):
        first = source_row(**{"注册率": "0.5"})
        second = source_row(**{"注册率": "0.6"})
        channel = self.write_csv(
            "A_2024-01-01_2026-06-02.csv", [list(HEADERS), first, second]
        )

        with self.assertRaisesRegex(
            DashboardLoadError,
            "duplicate_non_additive_conflict:market_dashboard_channel:registration_rate",
        ):
            load_market_dashboard_rows(
                self.overall_fixture(), (channel,), snapshot_id="s1"
            )

    def test_empty_overall_file_is_rejected_as_missing_observation(self):
        overall = self.write_csv(
            "大盘_2024-01-01_2026-06-02.csv", [list(HEADERS)]
        )

        with self.assertRaisesRegex(DashboardLoadError, "overall_source_has_no_data"):
            load_market_dashboard_rows(overall, (), snapshot_id="s1")

    def test_source_contract_and_runtime_bindings_publish_both_tables(self):
        source = yaml.safe_load(SOURCE_CONTRACT.read_text(encoding="utf-8"))
        runtime = yaml.safe_load(RUNTIME_BINDINGS.read_text(encoding="utf-8"))

        binding = source["runtime_binding"]
        self.assertEqual(binding["overall"]["dataset_id"], "market_dashboard")
        self.assertEqual(binding["overall"]["physical_table_prefix"], f"{OVERALL_TABLE}__")
        self.assertEqual(binding["channel"]["dataset_id"], "market_dashboard_channel")
        self.assertEqual(binding["channel"]["physical_table_prefix"], f"{CHANNEL_TABLE}__")
        self.assertTrue(runtime["datasets"]["market_dashboard"]["requires_release"])
        self.assertTrue(
            runtime["datasets"]["market_dashboard"]["requires_physical_revision"]
        )
        self.assertEqual(
            runtime["datasets"]["market_dashboard"]["physical_table_prefix"],
            f"{OVERALL_TABLE}__",
        )
        self.assertEqual(
            runtime["datasets"]["market_dashboard_channel"]["physical_table_prefix"],
            f"{CHANNEL_TABLE}__",
        )
        self.assertEqual(runtime["metrics"]["paid_amount"]["value_semantics"], "raw_scalar")
        self.assertEqual(runtime["metrics"]["paid_amount"]["display_format"], "number")


if __name__ == "__main__":
    unittest.main()
