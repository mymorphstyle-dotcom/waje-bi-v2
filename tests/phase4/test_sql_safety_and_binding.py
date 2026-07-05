import os
import unittest
from unittest.mock import patch

from bi_agent.runtime.sql_safety import validate_select_only
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime


class FakeQueryResult:
    column_names = ("pay_date", "amount")
    result_rows = (("2026-01-01", 10),)


class FakeClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, **kwargs):
        self.queries.append((sql, kwargs))
        return FakeQueryResult()


class SqlSafetyAndBindingTest(unittest.TestCase):
    def test_select_with_limit_is_allowed(self):
        result = validate_select_only(
            "SELECT pay_date, sum(amount) FROM paid_success GROUP BY pay_date LIMIT 10"
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.query_hash)

    def test_mutation_and_ddl_are_blocked(self):
        for sql in [
            "INSERT INTO x VALUES (1)",
            "DROP TABLE x",
            "ALTER TABLE x DELETE WHERE 1",
            "SELECT * FROM file('/tmp/x') LIMIT 1",
            "SELECT * FROM remote('127.0.0.1', db, table) LIMIT 1",
            "SELECT * FROM executable('cat /etc/passwd') LIMIT 1",
            "SELECT * FROM azureBlobStorage('https://example.test/blob') LIMIT 1",
            "SELECT * FROM azureBlobStorageCluster('cluster', 'https://example.test/blob') LIMIT 1",
            "SELECT * FROM clusterAllReplicas('cluster', db, table) LIMIT 1",
            "SELECT * FROM filesystem('/tmp/*.parquet') LIMIT 1",
            "SELECT * FROM hdfsCluster('cluster', 'hdfs://namenode/path') LIMIT 1",
            "SELECT * FROM hudi('s3://bucket/table') LIMIT 1",
            "SELECT * FROM iceberg('s3://bucket/table') LIMIT 1",
            "SELECT * FROM arrowFlight('host:port', 'dataset') LIMIT 1",
            "SELECT * FROM s3Cluster('cluster', 's3://bucket/key') LIMIT 1",
        ]:
            result = validate_select_only(sql)
            self.assertFalse(result.ok)
            self.assertTrue(result.reason)

    def test_export_and_runtime_settings_clauses_are_blocked(self):
        for sql in [
            "SELECT * FROM paid_success LIMIT 1 INTO OUTFILE '/tmp/x'",
            "SELECT * FROM paid_success LIMIT 1 FORMAT JSON",
            "SELECT * FROM paid_success LIMIT 1 SETTINGS max_threads = 1",
        ]:
            result = validate_select_only(sql)
            self.assertFalse(result.ok)
            self.assertTrue(result.reason)

    def test_aggregate_query_can_skip_limit_when_marked(self):
        result = validate_select_only("SELECT count(*) FROM paid_success", aggregate=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.query_hash)

    def test_select_without_limit_requires_aggregate_marker(self):
        result = validate_select_only("SELECT * FROM paid_success")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "limit_required")

    def test_nested_limit_does_not_satisfy_top_level_limit(self):
        result = validate_select_only(
            "SELECT * FROM paid_success WHERE EXISTS (SELECT 1 LIMIT 1)"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "limit_required")

    def test_aggregate_marker_does_not_allow_unbounded_detail_select(self):
        result = validate_select_only("SELECT * FROM paid_success", aggregate=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "aggregate_shape_required")

    def test_group_by_without_aggregate_does_not_satisfy_aggregate_shape(self):
        result = validate_select_only(
            "SELECT user_id FROM paid_success GROUP BY user_id", aggregate=True
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "aggregate_shape_required")

    def test_comments_are_stripped_before_select_check(self):
        result = validate_select_only("-- inspection query\nSELECT * FROM paid_success LIMIT 1")
        self.assertTrue(result.ok)

    def test_missing_clickhouse_env_returns_binding_failure(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = ClickHouseRuntime.from_env()

        self.assertFalse(runtime.configured())
        result = runtime.show_tables()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "runtime_binding_failed")

    def test_invalid_clickhouse_secure_env_fails_binding(self):
        env = {
            "WAJE_CLICKHOUSE_HOST": "localhost",
            "WAJE_CLICKHOUSE_PORT": "8123",
            "WAJE_CLICKHOUSE_USER": "reader",
            "WAJE_CLICKHOUSE_PASSWORD": "secret",
            "WAJE_CLICKHOUSE_DATABASE": "waje_bi",
            "WAJE_CLICKHOUSE_SECURE": "maybe",
        }
        with patch.dict(os.environ, env, clear=True):
            runtime = ClickHouseRuntime.from_env()

        self.assertFalse(runtime.configured())
        self.assertEqual(runtime.binding.reason, "invalid_clickhouse_secure")

    def test_show_describe_sample_and_aggregate_use_safe_queries(self):
        client = FakeClient()
        runtime = ClickHouseRuntime(
            host="localhost",
            port=8123,
            user="reader",
            password="secret",
            database="analytics",
            secure=False,
            client=client,
        )

        show_result = runtime.show_tables()
        describe_result = runtime.describe_table("paid_success")
        sample_result = runtime.sample_rows("paid_success", limit=2)
        aggregate_result = runtime.aggregate(
            "SELECT count(*) FROM paid_success", query_id="phase4_test"
        )

        self.assertTrue(show_result.ok)
        self.assertTrue(describe_result.ok)
        self.assertTrue(sample_result.ok)
        self.assertTrue(aggregate_result.ok)
        self.assertEqual(
            [query for query, _ in client.queries],
            [
                "SHOW TABLES",
                "DESCRIBE TABLE paid_success",
                "SELECT * FROM paid_success LIMIT 2",
                "SELECT count(*) FROM paid_success",
            ],
        )
        self.assertEqual(client.queries[-1][1]["query_id"], "phase4_test")

    def test_sample_rows_has_small_limit_cap(self):
        runtime = ClickHouseRuntime(client=FakeClient())

        result = runtime.sample_rows("paid_success", limit=1001)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "sample_limit_too_large")


if __name__ == "__main__":
    unittest.main()
