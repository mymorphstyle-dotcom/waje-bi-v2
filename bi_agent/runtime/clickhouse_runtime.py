from dataclasses import dataclass, field
import os
import re
from typing import Any, Optional

from bi_agent.runtime.sql_safety import validate_select_only


ENV_NAMES = (
    "WAJE_CLICKHOUSE_HOST",
    "WAJE_CLICKHOUSE_PORT",
    "WAJE_CLICKHOUSE_USER",
    "WAJE_CLICKHOUSE_PASSWORD",
    "WAJE_CLICKHOUSE_DATABASE",
    "WAJE_CLICKHOUSE_SECURE",
)

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


@dataclass(frozen=True)
class RuntimeBinding:
    ok: bool
    reason: str = ""
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClickHouseQueryResult:
    ok: bool
    reason: str = ""
    rows: tuple[Any, ...] = ()
    query_hash: str = ""
    query_id: str = ""


@dataclass
class ClickHouseRuntime:
    host: str = ""
    port: int = 0
    user: str = ""
    password: str = field(default="", repr=False)
    database: str = ""
    secure: bool = False
    binding: RuntimeBinding = field(default_factory=lambda: RuntimeBinding(ok=True))
    client: Optional[Any] = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "ClickHouseRuntime":
        values = {name: os.environ.get(name, "") for name in ENV_NAMES}
        missing = tuple(name for name, value in values.items() if not value)
        if missing:
            return cls(
                binding=RuntimeBinding(
                    ok=False, reason="missing_clickhouse_env", missing=missing
                )
            )

        try:
            port = int(values["WAJE_CLICKHOUSE_PORT"])
        except ValueError:
            return cls(
                binding=RuntimeBinding(ok=False, reason="invalid_clickhouse_port")
            )

        return cls(
            host=values["WAJE_CLICKHOUSE_HOST"],
            port=port,
            user=values["WAJE_CLICKHOUSE_USER"],
            password=values["WAJE_CLICKHOUSE_PASSWORD"],
            database=values["WAJE_CLICKHOUSE_DATABASE"],
            secure=_env_bool(values["WAJE_CLICKHOUSE_SECURE"]),
            binding=RuntimeBinding(ok=True),
        )

    def configured(self) -> bool:
        return self.binding.ok

    def show_tables(self) -> ClickHouseQueryResult:
        return self._execute_allowlisted("SHOW TABLES")

    def describe_table(self, table_name: str) -> ClickHouseQueryResult:
        if not _safe_identifier(table_name):
            return ClickHouseQueryResult(ok=False, reason="invalid_identifier")
        return self._execute_allowlisted(f"DESCRIBE TABLE {table_name}")

    def sample_rows(self, table_name: str, limit: int = 5) -> ClickHouseQueryResult:
        if not _safe_identifier(table_name):
            return ClickHouseQueryResult(ok=False, reason="invalid_identifier")
        if not isinstance(limit, int) or limit < 1:
            return ClickHouseQueryResult(ok=False, reason="invalid_limit")
        return self._execute_select(f"SELECT * FROM {table_name} LIMIT {limit}")

    def aggregate(self, sql: str, query_id: str) -> ClickHouseQueryResult:
        return self._execute_select(sql, query_id=query_id, aggregate=True)

    def _execute_allowlisted(
        self, sql: str, query_id: str = ""
    ) -> ClickHouseQueryResult:
        if sql != "SHOW TABLES" and not sql.startswith("DESCRIBE TABLE "):
            return ClickHouseQueryResult(ok=False, reason="unsupported_inspection_query")
        return self._execute(sql, query_id=query_id)

    def _execute_select(
        self, sql: str, *, query_id: str = "", aggregate: bool = False
    ) -> ClickHouseQueryResult:
        validation = validate_select_only(sql, aggregate=aggregate)
        if not validation.ok:
            return ClickHouseQueryResult(
                ok=False, reason=validation.reason, query_hash=validation.query_hash
            )
        return self._execute(sql, query_id=query_id, query_hash=validation.query_hash)

    def _execute(
        self, sql: str, *, query_id: str = "", query_hash: str = ""
    ) -> ClickHouseQueryResult:
        if not self.configured():
            return ClickHouseQueryResult(ok=False, reason="runtime_binding_failed")

        try:
            client = self._get_client()
            kwargs = {"query_id": query_id} if query_id else {}
            result = client.query(sql, **kwargs)
        except Exception:
            return ClickHouseQueryResult(ok=False, reason="clickhouse_query_failed")

        return ClickHouseQueryResult(
            ok=True,
            rows=_rows_from_result(result),
            query_hash=query_hash,
            query_id=query_id,
        )

    def _get_client(self) -> Any:
        if self.client is None:
            import clickhouse_connect

            self.client = clickhouse_connect.get_client(
                host=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                database=self.database,
                secure=self.secure,
            )
        return self.client


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_identifier(value: str) -> bool:
    return isinstance(value, str) and IDENTIFIER_PATTERN.match(value) is not None


def _rows_from_result(result: Any) -> tuple[Any, ...]:
    if hasattr(result, "named_results"):
        return tuple(result.named_results())

    rows = tuple(getattr(result, "result_rows", ()) or ())
    columns = tuple(getattr(result, "column_names", ()) or ())
    if columns:
        return tuple(dict(zip(columns, row)) for row in rows)
    return rows
