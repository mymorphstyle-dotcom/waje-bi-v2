from dataclasses import dataclass
import hashlib
import re
from typing import Any


BLOCKED_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "CREATE",
        "ATTACH",
        "DETACH",
        "SYSTEM",
        "KILL",
        "GRANT",
        "REVOKE",
    }
)

BLOCKED_TABLE_FUNCTIONS = frozenset(
    {
        "azureblobstorage",
        "executable",
        "file",
        "filecluster",
        "gcs",
        "hdfs",
        "jdbc",
        "mongodb",
        "mysql",
        "odbc",
        "postgresql",
        "redis",
        "remote",
        "remotesecure",
        "s3",
        "s3cluster",
        "sqlite",
        "url",
        "urlcluster",
    }
)

BLOCKED_SELECT_CLAUSES = frozenset({"FORMAT", "OUTFILE", "SETTINGS"})
AGGREGATE_FUNCTIONS = (
    "avg",
    "count",
    "countIf",
    "max",
    "min",
    "quantile",
    "sum",
    "sumIf",
    "uniq",
)
TABLE_FUNCTION_INTRODUCERS = frozenset({"FROM", "JOIN"})


@dataclass(frozen=True)
class SqlSafetyResult:
    ok: bool
    query_hash: str = ""
    reason: str = ""


def validate_select_only(sql: str, *, aggregate: bool = False) -> SqlSafetyResult:
    if not isinstance(sql, str):
        return SqlSafetyResult(ok=False, reason="invalid_sql")

    query_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    cleaned = _strip_comments(sql).strip()
    if not cleaned:
        return SqlSafetyResult(ok=False, query_hash=query_hash, reason="empty_query")
    if _has_internal_semicolon(cleaned):
        return SqlSafetyResult(
            ok=False, query_hash=query_hash, reason="multiple_statements"
        )

    cleaned = _strip_trailing_semicolon(cleaned)
    tokens = _tokens(cleaned)
    if not tokens:
        return SqlSafetyResult(ok=False, query_hash=query_hash, reason="empty_query")
    if tokens[0].upper() not in {"SELECT", "WITH"}:
        return SqlSafetyResult(ok=False, query_hash=query_hash, reason="select_only")

    upper_tokens = {token.upper() for token in tokens}
    if upper_tokens & BLOCKED_KEYWORDS:
        return SqlSafetyResult(
            ok=False, query_hash=query_hash, reason="blocked_keyword"
        )
    top_level_tokens = _top_level_tokens(cleaned)
    top_level_token_set = {token.upper() for token in top_level_tokens}

    if _has_blocked_table_function(cleaned) or _has_table_function(cleaned):
        return SqlSafetyResult(
            ok=False, query_hash=query_hash, reason="blocked_table_function"
        )
    if upper_tokens & BLOCKED_SELECT_CLAUSES:
        return SqlSafetyResult(
            ok=False, query_hash=query_hash, reason="blocked_select_clause"
        )
    has_top_level_limit = "LIMIT" in top_level_token_set
    if aggregate and not has_top_level_limit and not _has_aggregate_shape(cleaned):
        return SqlSafetyResult(
            ok=False, query_hash=query_hash, reason="aggregate_shape_required"
        )
    if not aggregate and not has_top_level_limit:
        return SqlSafetyResult(ok=False, query_hash=query_hash, reason="limit_required")

    return SqlSafetyResult(ok=True, query_hash=query_hash)


def _strip_comments(sql: str) -> str:
    output = []
    index = 0
    quote = None
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if quote:
            output.append(char)
            if char == "\\" and quote != "`" and index + 1 < len(sql):
                output.append(sql[index + 1])
                index += 2
                continue
            if char == quote:
                if quote == "'" and next_char == "'":
                    output.append(next_char)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "-" and next_char == "-":
            index = _skip_to_newline(sql, index + 2)
            output.append("\n")
            continue
        if char == "#":
            index = _skip_to_newline(sql, index + 1)
            output.append("\n")
            continue
        if char == "/" and next_char == "*":
            index = sql.find("*/", index + 2)
            if index == -1:
                return "".join(output)
            index += 2
            output.append(" ")
            continue

        output.append(char)
        index += 1
    return "".join(output)


def _skip_to_newline(sql: str, index: int) -> int:
    while index < len(sql) and sql[index] not in "\r\n":
        index += 1
    return index


def _strip_trailing_semicolon(sql: str) -> str:
    stripped = sql.strip()
    if stripped.endswith(";"):
        return stripped[:-1].strip()
    return stripped


def _has_internal_semicolon(sql: str) -> bool:
    masked = _mask_string_literals(sql).strip()
    if ";" not in masked:
        return False
    if not masked.endswith(";"):
        return True
    return ";" in masked[:-1]


def _tokens(sql: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _mask_string_literals(sql))


def _has_blocked_table_function(sql: str) -> bool:
    pattern = r"\b(" + "|".join(sorted(BLOCKED_TABLE_FUNCTIONS)) + r")\s*\("
    return re.search(pattern, _mask_string_literals(sql), re.IGNORECASE) is not None


def _has_table_function(sql: str) -> bool:
    pattern = r"\b(FROM|JOIN)\s+[A-Za-z_][A-Za-z0-9_]*\s*\("
    return re.search(pattern, _mask_string_literals(sql), re.IGNORECASE) is not None


def _has_aggregate_shape(sql: str) -> bool:
    masked = _mask_string_literals(sql)
    pattern = r"\b(" + "|".join(AGGREGATE_FUNCTIONS) + r")\s*\("
    return re.search(pattern, masked, re.IGNORECASE) is not None


def _top_level_tokens(sql: str) -> list[str]:
    masked = _mask_string_literals(sql)
    tokens = []
    depth = 0
    index = 0
    while index < len(masked):
        char = masked[index]
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and (char.isalpha() or char == "_"):
            start = index
            index += 1
            while index < len(masked) and (masked[index].isalnum() or masked[index] == "_"):
                index += 1
            tokens.append(masked[start:index])
            continue
        index += 1
    return tokens


def _mask_string_literals(sql: str) -> str:
    chars: list[str] = []
    index = 0
    quote: Any = None
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if quote:
            chars.append(" ")
            if char == "\\" and quote != "`" and index + 1 < len(sql):
                chars.append(" ")
                index += 2
                continue
            if char == quote:
                if quote == "'" and next_char == "'":
                    chars.append(" ")
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            chars.append(" ")
        else:
            chars.append(char)
        index += 1
    return "".join(chars)
