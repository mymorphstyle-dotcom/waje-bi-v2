from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from typing import Any, Mapping, Optional

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
MAX_SAMPLE_LIMIT = 1000


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
    provider_stats: Mapping[str, Any] = field(default_factory=dict)
    execution_attempt_ref: str = ""


class _RequiredQueryKwargUnsupported(Exception):
    def __init__(self, kwarg: str) -> None:
        super().__init__(kwarg)
        self.kwarg = kwarg


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
        secure = _env_bool(values["WAJE_CLICKHOUSE_SECURE"])
        if secure is None:
            return cls(
                binding=RuntimeBinding(ok=False, reason="invalid_clickhouse_secure")
            )

        return cls(
            host=values["WAJE_CLICKHOUSE_HOST"],
            port=port,
            user=values["WAJE_CLICKHOUSE_USER"],
            password=values["WAJE_CLICKHOUSE_PASSWORD"],
            database=values["WAJE_CLICKHOUSE_DATABASE"],
            secure=secure,
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
        if limit > MAX_SAMPLE_LIMIT:
            return ClickHouseQueryResult(ok=False, reason="sample_limit_too_large")
        return self._execute_select(f"SELECT * FROM {table_name} LIMIT {limit}")

    def aggregate(
        self,
        sql: str,
        query_id: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        execution_attempt_ref: str = "",
    ) -> ClickHouseQueryResult:
        aggregate_settings = dict(settings or {})
        aggregate_settings["result_overflow_mode"] = "throw"
        return self._execute_select(
            sql,
            query_id=query_id,
            aggregate=True,
            parameters=parameters,
            settings=aggregate_settings,
            execution_attempt_ref=execution_attempt_ref,
        )

    def _execute_allowlisted(
        self, sql: str, query_id: str = ""
    ) -> ClickHouseQueryResult:
        if sql != "SHOW TABLES" and not sql.startswith("DESCRIBE TABLE "):
            return ClickHouseQueryResult(ok=False, reason="unsupported_inspection_query")
        return self._execute(sql, query_id=query_id)

    def _execute_select(
        self,
        sql: str,
        *,
        query_id: str = "",
        aggregate: bool = False,
        parameters: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        execution_attempt_ref: str = "",
    ) -> ClickHouseQueryResult:
        validation = validate_select_only(sql, aggregate=aggregate)
        if not validation.ok:
            return ClickHouseQueryResult(
                ok=False,
                reason=validation.reason,
                query_hash=validation.query_hash,
                execution_attempt_ref=execution_attempt_ref,
            )
        query_hash = audit_query_hash(sql, parameters)
        return self._execute(
            sql,
            query_id=query_id,
            query_hash=query_hash,
            parameters=parameters,
            settings=settings,
            execution_attempt_ref=execution_attempt_ref,
        )

    def _execute(
        self,
        sql: str,
        *,
        query_id: str = "",
        query_hash: str = "",
        parameters: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        execution_attempt_ref: str = "",
    ) -> ClickHouseQueryResult:
        if not self.configured():
            return ClickHouseQueryResult(
                ok=False,
                reason="runtime_binding_failed",
                execution_attempt_ref=execution_attempt_ref,
            )

        try:
            client = self._get_client()
            kwargs = {"query_id": query_id} if query_id else {}
            if parameters is not None:
                kwargs["parameters"] = dict(parameters)
            if settings is not None:
                kwargs["settings"] = dict(settings)
            result, omitted_kwargs = _query_with_compatible_kwargs(client, sql, kwargs)
        except _RequiredQueryKwargUnsupported as exc:
            return ClickHouseQueryResult(
                ok=False,
                reason=f"clickhouse_required_kwarg_unsupported:{exc.kwarg}",
                query_hash=query_hash,
                query_id=query_id,
                provider_stats={"unsupported_kwarg": exc.kwarg},
                execution_attempt_ref=execution_attempt_ref,
            )
        except TypeError:
            raise
        except Exception as exc:
            return ClickHouseQueryResult(
                ok=False,
                reason=_clickhouse_failure_reason(exc),
                query_hash=query_hash,
                query_id=query_id,
                execution_attempt_ref=execution_attempt_ref,
            )

        provider_stats = _provider_stats(
            result,
            settings=settings,
            omitted_kwargs=omitted_kwargs,
        )
        if _provider_result_truncated(provider_stats):
            return ClickHouseQueryResult(
                ok=False,
                reason="clickhouse_result_truncated",
                query_hash=query_hash,
                query_id=query_id,
                provider_stats=provider_stats,
                execution_attempt_ref=execution_attempt_ref,
            )
        return ClickHouseQueryResult(
            ok=True,
            rows=_rows_from_result(result),
            query_hash=query_hash,
            query_id=query_id,
            provider_stats=provider_stats,
            execution_attempt_ref=execution_attempt_ref,
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


def _env_bool(value: str) -> Optional[bool]:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


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


def _query_with_compatible_kwargs(
    client: Any,
    sql: str,
    kwargs: Mapping[str, Any],
) -> tuple[Any, tuple[str, ...]]:
    active_kwargs = dict(kwargs)
    omitted: list[str] = []
    while True:
        try:
            return client.query(sql, **active_kwargs), tuple(omitted)
        except TypeError as exc:
            unsupported = _explicit_unexpected_kwarg(exc)
            if unsupported == "query_id" and unsupported in active_kwargs:
                active_kwargs.pop(unsupported)
                omitted.append(unsupported)
                continue
            if unsupported in {"parameters", "settings"} and unsupported in active_kwargs:
                raise _RequiredQueryKwargUnsupported(unsupported) from exc
            if not unsupported:
                raise
            raise


def _explicit_unexpected_kwarg(error: TypeError) -> str:
    match = re.search(
        r"unexpected keyword argument ['\"](query_id|parameters|settings)['\"]",
        str(error),
        re.IGNORECASE,
    )
    return match.group(1).casefold() if match is not None else ""


def _provider_stats(
    result: Any,
    *,
    settings: Mapping[str, Any] | None,
    omitted_kwargs: tuple[str, ...],
) -> dict[str, Any]:
    summary = getattr(result, "summary", None)
    stats = dict(summary) if isinstance(summary, Mapping) else {}
    provider_query_id = getattr(result, "query_id", "")
    if provider_query_id not in (None, ""):
        stats["provider_query_id"] = str(provider_query_id)
    if settings and "settings" not in omitted_kwargs:
        stats["requested_settings"] = dict(settings)
    if omitted_kwargs:
        stats["compatibility_omitted_kwargs"] = omitted_kwargs
    return stats


def _provider_result_truncated(provider_stats: Mapping[str, Any]) -> bool:
    for key in ("result_overflow_mode", "overflow_mode"):
        if str(provider_stats.get(key) or "").casefold() == "break":
            return True
    return provider_stats.get("truncated") is True


def _clickhouse_failure_reason(error: Exception) -> str:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return f"transient_clickhouse:{_exception_category(error)}"
    class_name = type(error).__name__.casefold()
    if any(
        token in class_name
        for token in (
            "connection",
            "network",
            "operational",
            "socket",
            "timeout",
            "transport",
        )
    ):
        return f"transient_clickhouse:{_exception_category(error)}"
    return "clickhouse_query_failed"


def _exception_category(error: Exception) -> str:
    name = type(error).__name__
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).casefold()


def audit_query_hash(sql: str, parameters: Mapping[str, Any] | None) -> str:
    payload = json.dumps(
        {"sql": sql, "parameters": _jsonable(parameters or {})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
