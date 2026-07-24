from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.durable_tool_bridge import PendingActionResolution
from bi_agent.runtime.general_agent_entry import (
    GeneralAgentTurnCommand,
    run_general_agent_turn,
)
from bi_agent.runtime.publication_persistence import _CustomerPayloadRecord
from tools.runtime.recover_run_dispatches import run_runtime_recovery_cycle


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "evals/general_agent_runtime/cases.jsonl"
CASE_SCHEMA_VERSION = "waje-standard-eval-case.v1"
REPORT_SCHEMA_VERSION = "waje-standard-pack-run.v1"
STANDARD_PACK_VERSION = "v1"
EXPECTED_CATEGORY_COUNTS = {
    "business": 24,
    "runtime": 12,
    "security": 4,
    "experience": 8,
}
VALID_PROFILES = frozenset({"smoke", "nightly", "release"})
VALID_ADAPTERS = frozenset({"agent_live", "pytest", "playwright"})
VALID_REVIEW_DIMENSIONS = frozenset(
    {
        "conclusion_directness",
        "analysis_completeness",
        "mechanisms_offsets_alternatives",
        "operational_meaning",
        "evidence_boundaries",
        "readability",
        "actionability",
    }
)
_HAN_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_NUMERIC_CLAIM = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?(?:%|亿|万|元|次|人)?"
)
_MARKDOWN_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MARKDOWN_TABLE_SEPARATOR = re.compile(
    r"^\s*:?-{3,}:?\s*$"
)
_HEADER_UNIT = re.compile(r"[（(]\s*(%|亿|万|元|次|人)\s*[）)]")


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        _validate_case(value, line_number)
        cases.append(value)
    if not cases:
        raise ValueError("eval_cases_missing")
    case_ids = [str(case["caseId"]) for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("eval_case_id_duplicate")
    return cases


def _validate_case(value: Any, line_number: int) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"eval_case_invalid:{line_number}")
    required = {
        "schemaVersion",
        "caseId",
        "title",
        "category",
        "questionFamily",
        "samplePool",
        "riskTier",
        "tags",
        "profiles",
        "execution",
        "failureAttribution",
    }
    allowed = required | {"fixture", "turns", "advisoryReview"}
    if set(value) - allowed or not required.issubset(value):
        raise ValueError(f"eval_case_fields_invalid:{line_number}")
    case_id = value.get("caseId")
    if (
        value.get("schemaVersion") != CASE_SCHEMA_VERSION
        or not isinstance(case_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_]{2,79}", case_id) is None
        or not _nonempty_text(value.get("title"))
        or value.get("category") not in EXPECTED_CATEGORY_COUNTS
        or value.get("samplePool")
        not in {"real_user", "historical_failure", "matrix_generated"}
        or value.get("riskTier") not in {"critical", "high", "medium"}
        or not _nonempty_text(value.get("questionFamily"))
    ):
        raise ValueError(f"eval_case_identity_invalid:{line_number}")
    _validate_string_set(value.get("tags"), line_number, "tags")
    profiles = _validate_string_set(value.get("profiles"), line_number, "profiles")
    if not set(profiles).issubset(VALID_PROFILES):
        raise ValueError(f"eval_case_profiles_invalid:{line_number}")
    execution = value.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError(f"eval_case_execution_invalid:{line_number}")
    execution_allowed = {
        "adapter",
        "target",
        "awaitTerminal",
        "terminalTimeoutSeconds",
        "releaseRepeats",
    }
    adapter = execution.get("adapter")
    repeats = execution.get("releaseRepeats")
    if (
        set(execution) - execution_allowed
        or adapter not in VALID_ADAPTERS
        or isinstance(repeats, bool)
        or not isinstance(repeats, int)
        or not 1 <= repeats <= 10
    ):
        raise ValueError(f"eval_case_execution_invalid:{line_number}")
    if adapter in {"pytest", "playwright"} and not _nonempty_text(
        execution.get("target")
    ):
        raise ValueError(f"eval_case_target_missing:{line_number}")
    if adapter == "agent_live":
        if value.get("riskTier") == "critical" and repeats != 3:
            raise ValueError(f"eval_case_live_repeats_invalid:{line_number}")
        _validate_live_fixture(value.get("fixture"), line_number)
        turns = value.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"eval_case_turns_missing:{line_number}")
        turn_ids: set[str] = set()
        for turn in turns:
            _validate_turn(turn, line_number)
            turn_id = str(turn["turnId"])
            if turn_id in turn_ids:
                raise ValueError(f"eval_case_turn_id_duplicate:{line_number}")
            turn_ids.add(turn_id)
    elif "fixture" in value or "turns" in value:
        raise ValueError(f"eval_case_adapter_payload_invalid:{line_number}")
    _validate_advisory_review(
        value.get("advisoryReview"),
        line_number,
        turn_ids={str(turn["turnId"]) for turn in value.get("turns") or []},
    )
    attribution = value.get("failureAttribution")
    if (
        not isinstance(attribution, Mapping)
        or set(attribution) != {"businessFailureType", "responsibilityPoint"}
        or not _nonempty_text(attribution.get("businessFailureType"))
        or attribution.get("responsibilityPoint")
        not in {
            "intent_plan",
            "capability_contract",
            "data_evidence",
            "claim_publication",
            "runtime_provider",
            "persistence_recovery",
            "customer_projection",
            "browser_experience",
            "eval_infrastructure",
        }
    ):
        raise ValueError(f"eval_case_attribution_invalid:{line_number}")


def _validate_live_fixture(value: Any, line_number: int) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"eval_case_fixture_invalid:{line_number}")
    allowed = {
        "threadMode",
        "completedThreadKey",
        "datasetReleaseRef",
        "metricContractVersion",
        "capabilityContractVersion",
    }
    if set(value) - allowed or value.get("threadMode") not in {
        "new",
        "completed_analysis",
    }:
        raise ValueError(f"eval_case_fixture_invalid:{line_number}")
    if value.get("threadMode") == "completed_analysis" and not _nonempty_text(
        value.get("completedThreadKey")
    ):
        raise ValueError(f"eval_case_completed_thread_key_missing:{line_number}")


def _validate_turn(value: Any, line_number: int) -> None:
    if not isinstance(value, Mapping) or set(value) - {
        "turnId",
        "message",
        "resolution",
        "expected",
    }:
        raise ValueError(f"eval_case_turn_invalid:{line_number}")
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9_]{1,39}", str(value.get("turnId") or ""))
        is None
        or not isinstance(value.get("expected"), Mapping)
        or not value["expected"]
    ):
        raise ValueError(f"eval_case_turn_invalid:{line_number}")
    has_message = _nonempty_text(value.get("message"))
    resolution = value.get("resolution")
    has_resolution = isinstance(resolution, Mapping)
    if has_message == has_resolution:
        raise ValueError(f"eval_case_turn_input_invalid:{line_number}")
    if has_resolution and (
        set(resolution) != {"kind"}
        or resolution.get("kind") != "recommended_option"
    ):
        raise ValueError(f"eval_case_turn_resolution_invalid:{line_number}")
    maximum_tool_call_count = value["expected"].get("maximumToolCallCount")
    if maximum_tool_call_count is not None and (
        isinstance(maximum_tool_call_count, bool)
        or not isinstance(maximum_tool_call_count, int)
        or maximum_tool_call_count < 0
    ):
        raise ValueError(f"eval_case_tool_call_count_invalid:{line_number}")


def _validate_advisory_review(
    value: Any,
    line_number: int,
    *,
    turn_ids: set[str],
) -> None:
    if value is None:
        return
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {"mode", "turnIds", "dimensions", "decisionCase", "reviewNote"}
        or value.get("mode") != "human_advisory"
        or not isinstance(value.get("decisionCase"), bool)
        or not _nonempty_text(value.get("reviewNote"))
    ):
        raise ValueError(f"eval_case_advisory_review_invalid:{line_number}")
    review_turn_ids = _validate_string_set(
        value.get("turnIds"), line_number, "review_turn_ids"
    )
    dimensions = _validate_string_set(
        value.get("dimensions"), line_number, "review_dimensions"
    )
    if not set(review_turn_ids).issubset(turn_ids) or not set(dimensions).issubset(
        VALID_REVIEW_DIMENSIONS
    ):
        raise ValueError(f"eval_case_advisory_review_invalid:{line_number}")
    if value["decisionCase"] and "actionability" not in dimensions:
        raise ValueError(f"eval_case_advisory_actionability_missing:{line_number}")


def _validate_string_set(value: Any, line_number: int, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or any(not _nonempty_text(item) for item in value)
    ):
        raise ValueError(f"eval_case_{field}_invalid:{line_number}")
    return [str(item) for item in value]


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _validate_catalog(cases: list[Mapping[str, Any]], *, complete: bool) -> None:
    completed_fixture_keys = [
        str((case.get("fixture") or {}).get("completedThreadKey"))
        for case in cases
        if (case.get("fixture") or {}).get("threadMode") == "completed_analysis"
    ]
    if len(completed_fixture_keys) != len(set(completed_fixture_keys)):
        raise ValueError("eval_completed_thread_fixture_key_reused")
    if not complete:
        return
    if len(cases) != 48:
        raise ValueError("standard_pack_case_count_invalid")
    if Counter(str(case["category"]) for case in cases) != Counter(
        EXPECTED_CATEGORY_COUNTS
    ):
        raise ValueError("standard_pack_category_coverage_invalid")
    if any("nightly" not in case["profiles"] or "release" not in case["profiles"] for case in cases):
        raise ValueError("standard_pack_profile_coverage_invalid")


def _create_thread(thread_id: str, actor_id: str) -> None:
    store = PostgresConversationStore.from_env()
    try:
        store.create_thread(thread_id, owner_id=actor_id)
    finally:
        store.connection.close()


def _inspect_operation(thread_id: str, operation_id: str) -> dict[str, Any]:
    store = PostgresConversationStore.from_env()
    try:
        ledger = store.thread_item_ledger
        selection_item = ledger.get_item_by_operation_key(
            thread_id,
            f"tool-selection:{operation_id}",
        )
        selection = (
            selection_item.payload.get("tool_selection", {})
            if selection_item is not None
            else {}
        )
        items = ledger.list_items(thread_id)
        tool_calls = [
            str(item.payload.get("sdk_item", {}).get("name") or "")
            for item in items
            if item.item_type == "tool_call"
            and str(item.operation_key or "").startswith(f"tool-call:{operation_id}:")
        ]
        operation_tool_results = [
            item
            for item in items
            if item.item_type == "tool_result"
            and str(item.operation_key or "").startswith(
                f"tool-result:{operation_id}:"
            )
        ]
        task_rows = store.connection.execute(
            """
            SELECT run_id, status
            FROM waje_runtime.analysis_runs
            WHERE thread_id = %(thread_id)s
            ORDER BY created_at, run_id
            """,
            {"thread_id": thread_id},
        ).fetchall()
        publication_rows = store.connection.execute(
            """
            SELECT customer.customer_payload_ref,
                   customer.publication_ref,
                   customer.customer_payload,
                   customer.payload
            FROM waje_runtime.publication_customer_payloads customer
            JOIN waje_runtime.analysis_runs run
              ON run.run_id = customer.run_attempt_id
            WHERE run.thread_id = %(thread_id)s
            ORDER BY customer.created_at, customer.customer_payload_ref
            """,
            {"thread_id": thread_id},
        ).fetchall()
        authority_refs: set[str] = set()
        authoritative_text: list[str] = []
        authoritative_publications: list[str] = []
        publication_integrity = True
        for row in publication_rows:
            customer_payload_ref = str(
                row.get("customer_payload_ref")
                if isinstance(row, Mapping)
                else row[0]
            )
            publication_ref = str(
                row.get("publication_ref") if isinstance(row, Mapping) else row[1]
            )
            customer_payload = (
                row.get("customer_payload") if isinstance(row, Mapping) else row[2]
            )
            payload = row.get("payload") if isinstance(row, Mapping) else row[3]
            try:
                record = _CustomerPayloadRecord.from_dict(payload)
                if record.customer_payload != customer_payload:
                    raise ValueError("customer_payload_column_mismatch")
            except Exception:
                publication_integrity = False
            authority_refs.update((customer_payload_ref, publication_ref))
            authoritative_publications.append(
                _publication_answer_text(customer_payload)
            )
            _collect_publication_authority(
                customer_payload,
                authority_refs=authority_refs,
                authoritative_text=authoritative_text,
            )
        for item in operation_tool_results:
            sdk_item = item.payload.get("sdk_item")
            if not isinstance(sdk_item, Mapping):
                continue
            output = sdk_item.get("output")
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except json.JSONDecodeError:
                    continue
            _collect_publication_authority(
                output,
                authority_refs=authority_refs,
                authoritative_text=authoritative_text,
            )
        terminal_item = ledger.get_item_by_operation_key(
            thread_id,
            f"terminal:{operation_id}",
        )
        assistant_item = ledger.get_item_by_operation_key(
            thread_id,
            f"assistant:{operation_id}",
        )
        final_output = (
            terminal_item.payload.get("final_output")
            if terminal_item is not None
            else None
        )
        return {
            "selection": dict(selection) if isinstance(selection, Mapping) else {},
            "toolCalls": [name for name in tool_calls if name],
            "tasks": [
                {
                    "taskRef": str(row.get("run_id") if isinstance(row, Mapping) else row[0]),
                    "status": str(row.get("status") if isinstance(row, Mapping) else row[1]),
                }
                for row in task_rows
            ],
            "fidelity": {
                "answerText": assistant_item.text if assistant_item is not None else "",
                "finalOutput": (
                    dict(final_output) if isinstance(final_output, Mapping) else None
                ),
                "authorityRefs": sorted(authority_refs),
                "authoritativeNumericClaims": sorted(
                    _numeric_claims("\n".join(authoritative_text))
                ),
                "publicationCount": len(publication_rows),
                "publicationIntegrity": publication_integrity,
                "authoritativePublicationText": (
                    authoritative_publications[-1]
                    if authoritative_publications
                    else ""
                ),
            },
        }
    finally:
        store.connection.close()


def _customer_text_is_zh(pending_action: Mapping[str, Any]) -> bool:
    values = [str(pending_action.get("prompt") or "")]
    for option in pending_action.get("options") or []:
        if isinstance(option, Mapping):
            values.extend((str(option.get("label") or ""), str(option.get("description") or "")))
    return bool(values) and all(_HAN_TEXT.search(value) for value in values)


def _collect_publication_authority(
    value: Any,
    *,
    authority_refs: set[str],
    authoritative_text: list[str],
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                key.endswith("_ref") or key.endswith("Ref")
            ) and isinstance(item, str) and item:
                authority_refs.add(item)
            if (key.endswith("_refs") or key.endswith("Refs")) and isinstance(item, list):
                authority_refs.update(
                    str(ref) for ref in item if isinstance(ref, str) and ref
                )
            if key in {
                "text",
                "value",
                "range_end",
                "customerSummary",
            } and isinstance(item, str):
                authoritative_text.append(item)
            _collect_publication_authority(
                item,
                authority_refs=authority_refs,
                authoritative_text=authoritative_text,
            )
    elif isinstance(value, list):
        for item in value:
            _collect_publication_authority(
                item,
                authority_refs=authority_refs,
                authoritative_text=authoritative_text,
            )


def _numeric_claims(value: str) -> set[str]:
    claims: set[str] = set()
    non_table_lines: list[str] = []
    lines = value.splitlines()
    index = 0
    while index < len(lines):
        if not _MARKDOWN_TABLE_ROW.fullmatch(lines[index]):
            non_table_lines.append(lines[index])
            index += 1
            continue
        table_lines: list[str] = []
        while index < len(lines) and _MARKDOWN_TABLE_ROW.fullmatch(lines[index]):
            table_lines.append(lines[index])
            index += 1
        if not _is_markdown_table(table_lines):
            non_table_lines.extend(table_lines)
            continue
        claims.update(_markdown_table_numeric_claims(table_lines))
    claims.update(_raw_numeric_claims("\n".join(non_table_lines)))
    return claims


def _raw_numeric_claims(value: str) -> set[str]:
    return {
        match.group(0).replace(",", "").removeprefix("+")
        for match in _NUMERIC_CLAIM.finditer(value)
    }


def _markdown_cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _is_markdown_table(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False
    header = _markdown_cells(lines[0])
    separator = _markdown_cells(lines[1])
    return len(header) == len(separator) and all(
        _MARKDOWN_TABLE_SEPARATOR.fullmatch(cell) for cell in separator
    )


def _markdown_table_numeric_claims(lines: list[str]) -> set[str]:
    headers = _markdown_cells(lines[0])
    units = [
        str(match.group(1)) if (match := _HEADER_UNIT.search(header)) else ""
        for header in headers
    ]
    claims: set[str] = set()
    for row in lines[2:]:
        cells = _markdown_cells(row)
        for column, cell in enumerate(cells):
            unit = units[column] if column < len(units) else ""
            for raw_claim in _raw_numeric_claims(cell):
                parsed = _numeric_value_unit_precision(raw_claim)
                if parsed is not None and not parsed[1] and unit:
                    claims.add(f"{raw_claim}{unit}")
                else:
                    claims.add(raw_claim)
    return claims


def _numeric_claim_is_source_supported(
    claim: str,
    source_claims: set[str],
    *,
    allow_derived_difference: bool,
) -> bool:
    if claim in source_claims:
        return True
    parsed = _numeric_value_unit_precision(claim)
    if parsed is None:
        return False
    value, unit, precision = parsed
    same_unit = [
        item
        for source in source_claims
        if (item := _numeric_value_unit_precision(source)) is not None
        and item[1] == unit
    ]
    if any(round(source_value, precision) == value for source_value, _, _ in same_unit):
        return True
    if not allow_derived_difference or not unit:
        return False
    return any(
        round(left_value - right_value, precision) == value
        for left_value, _, _ in same_unit
        for right_value, _, _ in same_unit
    )


def _numeric_value_unit_precision(value: str) -> tuple[float, str, int] | None:
    normalized = value.replace(",", "").removeprefix("+")
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(%|亿|万|元|次|人)?", normalized)
    if match is None:
        return None
    number = match.group(1)
    precision = len(number.partition(".")[2]) if "." in number else 0
    return float(number), str(match.group(2) or ""), precision


def _publication_answer_text(value: Any) -> str:
    if not isinstance(value, Mapping) or not isinstance(value.get("blocks"), list):
        return ""
    return "\n\n".join(
        str(block.get("text"))
        for block in value["blocks"]
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    )


def _advisory_review_package(
    review: Any,
    *,
    inspections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(review, Mapping):
        return None
    observations: list[dict[str, Any]] = []
    for turn_id in review["turnIds"]:
        inspection = inspections.get(str(turn_id)) or {}
        answer = str((inspection.get("fidelity") or {}).get("answerText") or "")
        paragraphs = [
            part.strip() for part in re.split(r"\n\s*\n", answer) if part.strip()
        ]
        observations.append(
            {
                "turnId": str(turn_id),
                "characterCount": len(answer),
                "paragraphCount": len(paragraphs),
                "hasAnswer": bool(answer.strip()),
            }
        )
    return {
        "mode": "human_advisory",
        "status": "pending_human_review",
        "dimensions": list(review["dimensions"]),
        "decisionCase": bool(review["decisionCase"]),
        "reviewNote": str(review.get("reviewNote") or ""),
        "observations": observations,
    }


def _evaluate(
    *,
    expected: Mapping[str, Any],
    result: Any,
    inspection: Mapping[str, Any],
    tasks_before: int,
    duration_seconds: float | None = None,
) -> list[str]:
    failures: list[str] = []
    selection = inspection.get("selection") or {}
    tool_calls = list(inspection.get("toolCalls") or [])
    tasks = list(inspection.get("tasks") or [])
    initial_action = selection.get("initialAction")
    required_tool = selection.get("requiredToolName")
    admission = result.terminal_admission
    completion_kind = admission.completion_kind if admission is not None else None
    published_context_recovery = bool(
        expected.get("allowPublishedContextRecovery")
        and not selection
        and not tool_calls
        and result.status == "completed_with_limits"
        and completion_kind == "context_response"
        and getattr(result, "error_code", None)
        in {
            "provider_authentication_failed",
            "provider_permission_denied",
            "provider_rate_limited",
            "provider_request_rejected",
            "provider_output_invalid",
            "provider_timeout",
            "provider_unavailable",
            "agents_sdk_runtime_failed",
        }
    )
    if expected.get("materialDecisionTopics") is not None and (
        selection.get("materialDecisionTopics") != expected["materialDecisionTopics"]
    ):
        failures.append("material_decision_topics_mismatch")
    if (
        not published_context_recovery
        and expected.get("initialAction")
        and initial_action != expected["initialAction"]
    ):
        failures.append("initial_action_mismatch")
    if (
        not published_context_recovery
        and expected.get("initialActionOneOf")
        and initial_action not in expected["initialActionOneOf"]
    ):
        failures.append("initial_action_not_allowed")
    if not published_context_recovery and expected.get("requiredTool"):
        wanted = expected["requiredTool"]
        if required_tool != wanted or not tool_calls or tool_calls[0] != wanted:
            failures.append("required_tool_mismatch")
    allowed_tools = expected.get("requiredToolOneOf") or []
    if not published_context_recovery and initial_action == "call_tool" and allowed_tools and (
        required_tool not in allowed_tools or not tool_calls or tool_calls[0] not in allowed_tools
    ):
        failures.append("required_tool_not_allowed")
    if set(tool_calls) & set(expected.get("forbiddenTools") or []):
        failures.append("forbidden_tool_called")
    maximum_tool_call_count = expected.get("maximumToolCallCount")
    if (
        maximum_tool_call_count is not None
        and len(tool_calls) > int(maximum_tool_call_count)
    ):
        failures.append("tool_call_count_exceeded")
    if expected.get("customerState") and result.status != expected["customerState"]:
        failures.append("customer_state_mismatch")
    if expected.get("customerStateOneOf") and result.status not in expected["customerStateOneOf"]:
        failures.append("customer_state_not_allowed")
    if expected.get("completionKind") and completion_kind != expected["completionKind"]:
        failures.append("completion_kind_mismatch")
    if expected.get("completionKindOneOf") and completion_kind not in expected["completionKindOneOf"]:
        failures.append("completion_kind_not_allowed")
    maximum_duration = expected.get("maximumDurationSeconds")
    if (
        maximum_duration is not None
        and duration_seconds is not None
        and duration_seconds > float(maximum_duration)
    ):
        failures.append("latency_target_exceeded")
    if expected.get("authorityRequired") and (
        admission is None or not admission.authority_refs
    ):
        failures.append("authority_refs_missing")
    projection = result.customer_projection()
    pending = projection.get("pendingAction") or {}
    options = pending.get("options") or []
    option_count = expected.get("optionCount")
    if option_count and not (
        int(option_count["minimum"]) <= len(options) <= int(option_count["maximum"])
    ):
        failures.append("option_count_invalid")
    recommended = sum(
        option.get("recommended") is True for option in options if isinstance(option, Mapping)
    )
    if expected.get("recommendedOptionCount") is not None and recommended != expected["recommendedOptionCount"]:
        failures.append("recommended_option_count_invalid")
    if expected.get("customerLanguage") == "zh-Hans" and not _customer_text_is_zh(pending):
        failures.append("customer_language_mismatch")
    checkpoint = result.checkpoint_item.payload.get("checkpoint", {}) if result.checkpoint_item else {}
    if expected.get("checkpointKind") and checkpoint.get("checkpointKind") != expected["checkpointKind"]:
        failures.append("checkpoint_kind_mismatch")
    if expected.get("checkpointSchema") and checkpoint.get("schemaVersion") != expected["checkpointSchema"]:
        failures.append("checkpoint_schema_mismatch")
    if expected.get("checkpointSchema") and not checkpoint.get("actionBindingDigest"):
        failures.append("checkpoint_action_binding_missing")
    if set(expected.get("forbiddenTools") or []) & {"run_bi_analysis", "continue_bi_analysis"}:
        if len(tasks) != tasks_before:
            failures.append("forbidden_task_created")
    fidelity_expected = expected.get("fidelity")
    if isinstance(fidelity_expected, Mapping):
        fidelity = inspection.get("fidelity") or {}
        final_output = fidelity.get("finalOutput") or {}
        material_refs = list(final_output.get("materialRefs") or [])
        if len(material_refs) < int(fidelity_expected.get("minimumMaterialRefs") or 0):
            failures.append("factual_material_refs_insufficient")
        answer_claims = _numeric_claims(str(fidelity.get("answerText") or ""))
        minimum_numeric = int(fidelity_expected.get("minimumNumericClaims") or 0)
        if len(answer_claims) < minimum_numeric:
            failures.append("factual_numeric_claims_insufficient")
        numeric_mode = fidelity_expected.get("numericClaims")
        if numeric_mode in {
            "published_source_subset",
            "published_or_derived_source",
        }:
            source_claims = set(fidelity.get("authoritativeNumericClaims") or [])
            if any(
                not _numeric_claim_is_source_supported(
                    claim,
                    source_claims,
                    allow_derived_difference=(
                        numeric_mode == "published_or_derived_source"
                    ),
                )
                for claim in answer_claims
            ):
                failures.append("factual_numeric_claim_unsupported")
        if fidelity_expected.get("requirePublicationIntegrity") and (
            int(fidelity.get("publicationCount") or 0) < 1
            or fidelity.get("publicationIntegrity") is not True
        ):
            failures.append("publication_fidelity_invalid")
        if fidelity_expected.get("answerMode") == "publication_exact" and (
            str(fidelity.get("answerText") or "")
            != str(fidelity.get("authoritativePublicationText") or "")
        ):
            failures.append("publication_answer_mismatch")
    return failures


async def _await_terminal_result(
    *,
    command: GeneralAgentTurnCommand,
    result: Any,
    timeout_seconds: int,
    worker_id: str,
) -> tuple[Any, int]:
    started = time.monotonic()
    current = result
    recovery_cycles = 0
    while current.status == "working":
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError("standard_pack_live_terminal_timeout")
        await asyncio.to_thread(
            run_runtime_recovery_cycle,
            limit=100,
            worker_id=worker_id,
            thread_id=command.thread_id,
        )
        recovery_cycles += 1
        current = await run_general_agent_turn(command)
        if current.status == "working":
            await asyncio.sleep(0.5)
    return current, recovery_cycles


def _recommended_resolution(result: Any) -> tuple[str, PendingActionResolution]:
    pending = result.customer_projection().get("pendingAction") or {}
    recommended = next(
        (
            option
            for option in pending.get("options") or []
            if isinstance(option, Mapping) and option.get("recommended") is True
        ),
        None,
    )
    if not isinstance(recommended, Mapping):
        raise ValueError("recommended_follow_up_missing")
    answer = str(recommended.get("label") or "").strip()
    if not answer:
        raise ValueError("recommended_follow_up_label_missing")
    return answer, PendingActionResolution(
        actionRef=str(pending["actionRef"]),
        decision="answered",
        selectedOptionId=str(recommended["optionId"]),
        answerText=answer,
    )


async def _run_agent_live_case(
    case: Mapping[str, Any],
    *,
    actor_id: str,
    fixture_map: Mapping[str, str],
    run_ref: str,
    repeat_index: int,
) -> dict[str, Any]:
    case_id = str(case["caseId"])
    fixture = case["fixture"]
    if fixture.get("threadMode") == "completed_analysis":
        fixture_key = str(fixture["completedThreadKey"])
        thread_id = str(fixture_map.get(fixture_key) or "")
        if not thread_id:
            raise ValueError(f"completed_analysis_thread_required:{fixture_key}")
    else:
        thread_id = f"thread-eval-{case_id}-{run_ref}-r{repeat_index}"
        _create_thread(thread_id, actor_id)
    failures: list[str] = []
    turn_reports: list[dict[str, Any]] = []
    inspections: dict[str, Mapping[str, Any]] = {}
    previous_result: Any | None = None
    for turn_index, turn in enumerate(case["turns"], 1):
        turn_started = time.monotonic()
        turn_id = str(turn["turnId"])
        operation_id = f"eval-{case_id}-{run_ref}-r{repeat_index}-t{turn_index}"
        before = _inspect_operation(thread_id, operation_id)
        pending_resolution: PendingActionResolution | None = None
        if "message" in turn:
            message = str(turn["message"])
        else:
            if previous_result is None:
                raise ValueError("recommended_follow_up_without_previous_turn")
            message, pending_resolution = _recommended_resolution(previous_result)
        command = GeneralAgentTurnCommand(
            threadId=thread_id,
            actorId=actor_id,
            operationId=operation_id,
            message=message,
            pendingActionResolution=pending_resolution,
        )
        result = await run_general_agent_turn(command)
        recovery_cycles = 0
        if case["execution"].get("awaitTerminal") and result.status == "working":
            result, recovery_cycles = await _await_terminal_result(
                command=command,
                result=result,
                timeout_seconds=int(
                    case["execution"].get("terminalTimeoutSeconds") or 1800
                ),
                worker_id=f"standard-pack-{run_ref}-{case_id}-r{repeat_index}",
            )
        inspection = _inspect_operation(thread_id, operation_id)
        inspections[turn_id] = inspection
        duration_seconds = round(time.monotonic() - turn_started, 3)
        turn_failures = _evaluate(
            expected=turn["expected"],
            result=result,
            inspection=inspection,
            tasks_before=len(before["tasks"]),
            duration_seconds=duration_seconds,
        )
        failures.extend(f"{turn_id}:{failure}" for failure in turn_failures)
        projection = result.customer_projection()
        turn_reports.append(
            {
                "turnId": turn_id,
                "runtimeStatus": result.status,
                "completionKind": projection.get("completionKind"),
                "selection": inspection["selection"],
                "toolCalls": inspection["toolCalls"],
                "taskCount": len(inspection["tasks"]),
                "durationSeconds": duration_seconds,
                "recoveryCycleCount": recovery_cycles,
                "customerMessage": str(
                    (projection.get("message") or {}).get("text") or ""
                ),
                "failures": turn_failures,
                "fidelityObservation": {
                    "authorityRefCount": len(
                        (inspection.get("fidelity") or {}).get("authorityRefs") or []
                    ),
                    "publicationCount": int(
                        (inspection.get("fidelity") or {}).get("publicationCount") or 0
                    ),
                    "publicationIntegrity": (
                        inspection.get("fidelity") or {}
                    ).get("publicationIntegrity"),
                },
            }
        )
        previous_result = result
    return {
        "caseId": case_id,
        "adapter": "agent_live",
        "threadId": thread_id,
        "hardStatus": "passed" if not failures else "failed",
        "failures": failures,
        "turns": turn_reports,
        "advisoryReview": _advisory_review_package(
            case.get("advisoryReview"),
            inspections=inspections,
        ),
    }


def _subprocess_output(value: bytes) -> str:
    rendered = value.decode("utf-8", errors="replace")
    for env_name in ("WAJE_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        secret = os.environ.get(env_name)
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    return rendered[-8000:]


async def _run_subprocess_case(
    case: Mapping[str, Any],
    *,
    artifact_dir: Path,
) -> dict[str, Any]:
    adapter = str(case["execution"]["adapter"])
    target = str(case["execution"]["target"])
    if adapter == "pytest":
        argv = [sys.executable, "-m", "pytest", "-q", target]
    elif adapter == "playwright":
        test_file, separator, grep_title = target.partition("#")
        if not separator or not test_file or not grep_title:
            raise ValueError("playwright_target_invalid")
        argv = [
            "npx",
            "playwright",
            "test",
            test_file,
            "--grep",
            grep_title,
            "--reporter=line",
        ]
    else:
        raise ValueError("standard_pack_subprocess_adapter_invalid")
    started = time.monotonic()
    process_env = dict(os.environ)
    case_artifact_dir: Path | None = None
    if adapter == "playwright":
        case_artifact_dir = artifact_dir / str(case["caseId"])
        case_artifact_dir.mkdir(parents=True, exist_ok=True)
        process_env["WAJE_VISUAL_EVIDENCE_DIR"] = str(case_artifact_dir)
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(ROOT),
        env=process_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    status = "passed" if process.returncode == 0 else "failed"
    artifact_refs = (
        [str(path) for path in sorted(case_artifact_dir.glob("**/*")) if path.is_file()]
        if case_artifact_dir is not None
        else []
    )
    return {
        "caseId": str(case["caseId"]),
        "adapter": adapter,
        "hardStatus": status,
        "failures": [] if status == "passed" else [f"{adapter}_target_failed"],
        "durationSeconds": round(time.monotonic() - started, 3),
        "exitCode": process.returncode,
        "output": _subprocess_output(output),
        "artifactRefs": artifact_refs,
    }


def _load_fixture_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, Mapping)
        or any(not _nonempty_text(key) or not _nonempty_text(item) for key, item in value.items())
        or len(set(value.values())) != len(value)
    ):
        raise ValueError("standard_pack_fixture_map_invalid")
    return {str(key): str(item) for key, item in value.items()}


def _provider_origin_for_live_cases(cases: list[Mapping[str, Any]]) -> str | None:
    if not any(case["execution"]["adapter"] == "agent_live" for case in cases):
        return None
    origin = urlparse(os.environ.get("WAJE_LLM_BASE_URL", ""))
    if origin.scheme not in {"http", "https"} or origin.hostname in {
        None,
        "api.openai.com",
    }:
        raise ValueError("eval_provider_outbound_origin_invalid")
    if not os.environ.get("WAJE_RUNTIME_DATABASE_URL"):
        raise ValueError("standard_pack_runtime_database_url_missing")
    return f"{origin.scheme}://{origin.netloc}"


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.database_url:
        os.environ["WAJE_RUNTIME_DATABASE_URL"] = args.database_url
    os.environ.pop("OPENAI_API_KEY", None)
    cases = _load_cases(args.cases)
    _validate_catalog(cases, complete=args.cases.resolve() == DEFAULT_CASES.resolve())
    selected = set(args.case_id or [])
    if selected:
        unknown = selected - {str(case["caseId"]) for case in cases}
        if unknown:
            raise ValueError(f"standard_pack_case_unknown:{','.join(sorted(unknown))}")
        cases = [case for case in cases if case["caseId"] in selected]
    else:
        cases = [case for case in cases if args.profile in case["profiles"]]
    if args.adapter:
        cases = [case for case in cases if case["execution"]["adapter"] in args.adapter]
    if not cases:
        raise ValueError("standard_pack_selection_empty")
    adapter_counts = Counter(str(case["execution"]["adapter"]) for case in cases)
    if args.validate_only:
        return {
            "schemaVersion": REPORT_SCHEMA_VERSION,
            "standardPackVersion": STANDARD_PACK_VERSION,
            "status": "passed",
            "mode": "catalog_validation",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "profile": args.profile,
            "catalogCaseCount": len(_load_cases(args.cases)),
            "selectedCaseCount": len(cases),
            "categoryCounts": dict(
                sorted(Counter(str(case["category"]) for case in cases).items())
            ),
            "adapterCounts": dict(sorted(adapter_counts.items())),
        }
    outbound_origin = _provider_origin_for_live_cases(cases)
    fixture_map = _load_fixture_map(args.fixture_map)
    required_fixture_keys = {
        str(case["fixture"]["completedThreadKey"])
        for case in cases
        if case["execution"]["adapter"] == "agent_live"
        and case["fixture"]["threadMode"] == "completed_analysis"
    }
    missing_fixture_keys = required_fixture_keys - set(fixture_map)
    if missing_fixture_keys:
        raise ValueError(
            "standard_pack_fixture_keys_missing:"
            + ",".join(sorted(missing_fixture_keys))
        )
    run_ref = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + uuid4().hex[:6]
    results: list[dict[str, Any]] = []
    for case in cases:
        repeats = (
            int(case["execution"]["releaseRepeats"])
            if args.profile == "release"
            else 1
        )
        repeat_results: list[dict[str, Any]] = []
        for repeat_index in range(1, repeats + 1):
            try:
                if case["execution"]["adapter"] == "agent_live":
                    repeat_results.append(
                        await _run_agent_live_case(
                            case,
                            actor_id=args.actor_id,
                            fixture_map=fixture_map,
                            run_ref=run_ref,
                            repeat_index=repeat_index,
                        )
                    )
                else:
                    repeat_results.append(
                        await _run_subprocess_case(
                            case,
                            artifact_dir=(
                                args.artifact_dir
                                or ROOT / "output/playwright/standard-pack" / run_ref
                            ),
                        )
                    )
            except Exception as exc:
                repeat_results.append(
                    {
                        "caseId": case["caseId"],
                        "adapter": case["execution"]["adapter"],
                        "hardStatus": "failed",
                        "failures": [
                            str(getattr(exc, "code", "") or str(exc) or type(exc).__name__)
                        ],
                    }
                )
        case_passed = all(item["hardStatus"] == "passed" for item in repeat_results)
        results.append(
            {
                "caseId": str(case["caseId"]),
                "category": str(case["category"]),
                "adapter": str(case["execution"]["adapter"]),
                "riskTier": str(case["riskTier"]),
                "hardStatus": "passed" if case_passed else "failed",
                "repeatCount": repeats,
                "repeats": repeat_results,
            }
        )
    passed = sum(result["hardStatus"] == "passed" for result in results)
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "standardPackVersion": STANDARD_PACK_VERSION,
        "status": "passed" if passed == len(results) else "failed",
        "mode": "execution",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile,
        "provider": os.environ.get("WAJE_LLM_PROVIDER", ""),
        "model": os.environ.get("WAJE_LLM_MODEL", ""),
        "outboundOrigin": outbound_origin,
        "openAiApiKeyPresent": False,
        "openAiHostedRequestCount": 0,
        "adapterCounts": dict(sorted(adapter_counts.items())),
        "caseCount": len(results),
        "passedCaseCount": passed,
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run WAJE Standard Pack v1")
    parser.add_argument("--database-url")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--fixture-map", type=Path)
    parser.add_argument("--actor-id", default="local-user")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--adapter", action="append", choices=sorted(VALID_ADAPTERS))
    parser.add_argument("--profile", choices=sorted(VALID_PROFILES), default="smoke")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
