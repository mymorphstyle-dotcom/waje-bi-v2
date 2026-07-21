from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.phase4.test_market_dashboard_ingestion import HEADERS, source_row
from tools.data.load_market_dashboard_clickhouse import (
    CHANNEL_TABLE,
    OVERALL_TABLE,
    DashboardLoadError,
    _prepare_clickhouse_schema,
    load_market_dashboard_rows,
    stage_market_dashboard_release,
    validate_clickhouse_schema,
    validate_persisted_snapshot,
)


class QueryResult:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def named_results(self):
        return iter(self.rows)


class VersionedClickHouseClient:
    def __init__(self, *, fail_channel_insert=False):
        self.tables = {OVERALL_TABLE: [], CHANNEL_TABLE: []}
        self.fail_channel_insert = fail_channel_insert
        self.insert_calls = 0

    def command(self, query, parameters=None, settings=None):
        if query.startswith("DELETE FROM"):
            table = query.split()[2]
            revision = parameters["load_revision"]
            self.tables[table] = [
                row
                for row in self.tables.get(table, ())
                if row["load_revision"] != revision
            ]
        return ""

    def raw_insert(self, table, *, insert_block=None, **kwargs):
        self.insert_calls += 1
        if table.startswith(CHANNEL_TABLE) and self.fail_channel_insert:
            raise RuntimeError("second insert failed")
        self.tables.setdefault(table, []).extend(
            json.loads(line)
            for line in insert_block.decode("utf-8").splitlines()
            if line
        )

    def query(self, query, parameters=None, settings=None):
        if "system.columns" in query:
            return QueryResult(self.schema_rows)
        if "system.tables" in query:
            return QueryResult(self.table_rows)
        table = query.split("FROM", 1)[1].split()[0]
        rows = [
            row
            for row in self.tables.get(table, ())
            if row["snapshot_id"] == parameters["snapshot_id"]
            and row["load_revision"] == parameters["load_revision"]
        ]
        return QueryResult(
            sorted(rows, key=lambda row: tuple(str(value) for value in row.values()))
        )


class MarketDashboardReleaseSafetyTest(unittest.TestCase):
    def test_schema_rebuild_never_drops_active_physical_tables(self):
        class FailingCreateClient:
            def __init__(self):
                self.commands = []

            def command(self, query, parameters=None, settings=None):
                self.commands.append(query)
                if query.startswith("EXISTS TABLE"):
                    return "0"
                if query.startswith("CREATE TABLE"):
                    raise RuntimeError("injected create failure")
                return ""

        client = FailingCreateClient()

        with self.assertRaisesRegex(RuntimeError, "injected create failure"):
            _prepare_clickhouse_schema(client, rebuild=True, container_mode=True)

        self.assertFalse(
            any(command.startswith("DROP TABLE") for command in client.commands)
        )

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.overall = root / "大盘_2024-01-01_2026-06-02.csv"
        self.channel = root / "A_2024-01-01_2026-06-02.csv"
        content = (
            "\n".join(
                (
                    ",".join(HEADERS),
                    ",".join(source_row(**{"付费金额": "3000"})),
                )
            )
            + "\n"
        )
        self.overall.write_text(content, encoding="utf-8-sig")
        self.channel.write_text(content, encoding="utf-8-sig")

    def tearDown(self):
        self.tempdir.cleanup()

    def parsed(self):
        return load_market_dashboard_rows(
            self.overall,
            (self.channel,),
            snapshot_id="logical-dashboard",
        )

    def test_second_table_failure_preserves_old_active_revision(self):
        rows, manifest = self.parsed()
        client = VersionedClickHouseClient(fail_channel_insert=True)
        old = {
            "snapshot_id": "logical-dashboard",
            "load_revision": "load:old",
            "business_date": "2026-06-01",
            "game": "Waje Special",
            "paid_amount": "1.000000000000",
        }
        client.tables[OVERALL_TABLE].append(dict(old))
        client.tables[CHANNEL_TABLE].append({**old, "channel": "A"})

        with self.assertRaisesRegex(RuntimeError, "second insert failed"):
            stage_market_dashboard_release(
                client,
                rows,
                manifest,
                active_load_revisions=("load:old",),
            )

        self.assertTrue(
            any(
                row["load_revision"] == "load:old"
                for row in client.tables[OVERALL_TABLE]
            )
        )
        self.assertTrue(
            any(
                row["load_revision"] == "load:old"
                for row in client.tables[CHANNEL_TABLE]
            )
        )
        self.assertTrue(
            any(
                row["load_revision"] == manifest.load_revision
                for row in client.tables[manifest.physical_table]
            )
        )
        self.assertFalse(
            any(
                row["load_revision"] == manifest.load_revision
                for row in client.tables.get(manifest.channel_physical_table, ())
            )
        )

    def test_partial_unreferenced_revision_retries_and_identical_release_skips(self):
        rows, manifest = self.parsed()
        client = VersionedClickHouseClient(fail_channel_insert=True)
        with self.assertRaises(RuntimeError):
            stage_market_dashboard_release(
                client, rows, manifest, active_load_revisions=()
            )
        client.fail_channel_insert = False

        status = stage_market_dashboard_release(
            client, rows, manifest, active_load_revisions=()
        )
        insert_calls = client.insert_calls
        second_status = stage_market_dashboard_release(
            client, rows, manifest, active_load_revisions=(manifest.load_revision,)
        )

        self.assertEqual(status, "staged_and_validated")
        self.assertEqual(second_status, "already_validated")
        self.assertEqual(client.insert_calls, insert_calls)
        self.assertEqual(
            sum(
                row["load_revision"] == manifest.load_revision
                for row in client.tables[manifest.physical_table]
            ),
            1,
        )
        self.assertEqual(
            sum(
                row["load_revision"] == manifest.load_revision
                for row in client.tables[manifest.channel_physical_table]
            ),
            1,
        )

    def test_all_column_corruption_fails_persisted_hash_validation(self):
        rows, manifest = self.parsed()
        client = VersionedClickHouseClient()
        stage_market_dashboard_release(client, rows, manifest, active_load_revisions=())
        staged = next(
            row
            for row in client.tables[manifest.physical_table]
            if row["load_revision"] == manifest.load_revision
        )
        staged["active_users"] = "999999"

        with self.assertRaisesRegex(DashboardLoadError, "persisted_rows_hash_mismatch"):
            validate_persisted_snapshot(client, rows, manifest)

    def test_schema_drift_fails_before_existing_tables_are_reused(self):
        client = VersionedClickHouseClient()
        client.schema_rows = (
            {
                "table": OVERALL_TABLE,
                "name": "paid_amount",
                "type": "Float64",
                "position": 1,
            },
        )
        client.table_rows = (
            {
                "name": OVERALL_TABLE,
                "engine": "MergeTree",
                "sorting_key": "snapshot_id",
            },
        )

        with self.assertRaisesRegex(DashboardLoadError, "clickhouse_schema_drift"):
            validate_clickhouse_schema(client)


if __name__ == "__main__":
    unittest.main()
