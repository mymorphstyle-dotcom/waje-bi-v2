import os
import unittest
from unittest.mock import patch

from clickhouse_connect.driver.exceptions import OperationalError

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


class FakeFailingClient:
    def query(self, sql, **kwargs):
        raise OperationalError("connection failed")


class FakeNoQueryIdClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, *, parameters=None, settings=None):
        self.queries.append(
            {
                "sql": sql,
                "parameters": parameters,
                "settings": settings,
            }
        )
        return FakeQueryResult()


class FakeRejectedRequiredKwargClient:
    def __init__(self, rejected_kwarg):
        self.rejected_kwarg = rejected_kwarg
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        raise TypeError(
            f"query() got an unexpected keyword argument '{self.rejected_kwarg}'"
        )


class FakeBroadSettingsTypeErrorClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        raise TypeError("unsupported parameter type in settings")


class FakeBreakResult(FakeQueryResult):
    summary = {"result_overflow_mode": "break", "read_rows": 100}


class FakeBreakClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        return FakeBreakResult()


class SqlSafetyAndBindingTest(unittest.TestCase):
    def test_driver_signature_mismatch_propagates_without_retry(self):
        for rejected_kwarg in ("parameters", "settings"):
            with self.subTest(rejected_kwarg=rejected_kwarg):
                client = FakeRejectedRequiredKwargClient(rejected_kwarg)
                runtime = ClickHouseRuntime(client=client)

                with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
                    runtime.aggregate(
                        "SELECT count() FROM paid_success WHERE status = %(status)s",
                        query_id="required-kwargs",
                        parameters={"status": "paid"},
                        settings={"readonly": 2},
                    )
                self.assertEqual(len(client.calls), 1)

    def test_broad_settings_type_error_is_not_a_compatibility_signal(self):
        client = FakeBroadSettingsTypeErrorClient()
        runtime = ClickHouseRuntime(client=client)

        with self.assertRaisesRegex(
            TypeError, "unsupported parameter type in settings"
        ):
            runtime.aggregate(
                "SELECT count() FROM paid_success",
                query_id="broad-type-error",
                parameters={},
                settings={"readonly": 2},
            )

        self.assertEqual(len(client.calls), 1)

    def test_provider_break_overflow_is_never_accepted_as_success(self):
        client = FakeBreakClient()
        runtime = ClickHouseRuntime(client=client)

        result = runtime.aggregate(
            "SELECT count() FROM paid_success",
            query_id="overflow-break",
            parameters={},
            settings={"readonly": 2},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "clickhouse_result_truncated")
        self.assertEqual(result.provider_stats["result_overflow_mode"], "break")
        self.assertEqual(len(client.calls), 1)

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
        result = validate_select_only(
            "SELECT count(*) FROM paid_success", aggregate=True
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.query_hash)

    def test_reviewed_clickhouse_aggregate_variants_are_recognized(self):
        for expression in (
            "uniqExact(order_id)",
            "uniqExactIf(order_id, status = 'paid')",
            "quantileExact(0.95)(amount)",
        ):
            with self.subTest(expression=expression):
                result = validate_select_only(
                    f"SELECT {expression} FROM paid_success",
                    aggregate=True,
                )
                self.assertTrue(result.ok, result.reason)

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

    def test_union_branch_limit_does_not_satisfy_global_bound(self):
        result = validate_select_only(
            "SELECT event_id FROM events "
            "UNION ALL SELECT event_id FROM sentinels LIMIT 5001"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "global_limit_required")

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
        result = validate_select_only(
            "-- inspection query\nSELECT * FROM paid_success LIMIT 1"
        )
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
        self.assertEqual(
            client.queries[-1][1]["settings"]["query_id"],
            "phase4_test",
        )

    def test_clickhouse_query_failure_preserves_hash_and_query_id(self):
        runtime = ClickHouseRuntime(
            host="localhost",
            port=8123,
            user="reader",
            password="secret",
            database="analytics",
            secure=False,
            client=FakeFailingClient(),
        )

        result = runtime.aggregate(
            "SELECT count(*) FROM paid_success", query_id="phase4_failure"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "transient_clickhouse:operational_error")
        self.assertTrue(result.query_hash)
        self.assertEqual(result.query_id, "phase4_failure")

    def test_query_id_uses_the_current_clickhouse_transport_setting_contract(self):
        client = FakeNoQueryIdClient()
        runtime = ClickHouseRuntime(
            host="localhost",
            port=8123,
            user="reader",
            password="secret",
            database="analytics",
            secure=False,
            client=client,
        )

        result = runtime.aggregate(
            "SELECT count(*) FROM paid_success",
            query_id="phase4_transport",
            settings={"query_id": "caller_supplied_id", "readonly": 2},
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(client.queries), 1)
        self.assertEqual(
            client.queries[0]["settings"],
            {
                "query_id": "phase4_transport",
                "readonly": 2,
                "result_overflow_mode": "throw",
            },
        )

    def test_sample_rows_has_small_limit_cap(self):
        runtime = ClickHouseRuntime(client=FakeClient())

        result = runtime.sample_rows("paid_success", limit=1001)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "sample_limit_too_large")


if __name__ == "__main__":
    unittest.main()
