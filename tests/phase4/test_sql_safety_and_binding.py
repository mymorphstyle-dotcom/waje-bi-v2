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
            "SELECT * FROM file('/tmp/x')",
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


if __name__ == "__main__":
    unittest.main()
