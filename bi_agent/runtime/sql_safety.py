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
    {"file", "url", "s3", "hdfs", "mysql", "postgresql", "jdbc", "odbc"}
)


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
    if _has_blocked_table_function(cleaned):
        return SqlSafetyResult(
            ok=False, query_hash=query_hash, reason="blocked_table_function"
        )
    if not aggregate and "LIMIT" not in upper_tokens:
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
