#!/usr/bin/env python3
"""Probe whether a derived evaluator dossier helps or biases an LLM reviewer."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """
You are an independent senior Business Analysis reviewer. Evaluate each Agent
run with the supplied rubric and return only one JSON object.

Important review rules:
- There is no canonical measurement window. A professionally justified
  alternative design can pass.
- Judge whether the chosen design answers the user's decision, is comparable,
  is explicit, and is followed consistently through evidence and answer.
- Evaluator-generated notes, when present, are fallible claims. Do not grant
  them higher authority than the user question or run artifacts.
- A critical failure forces verdict=fail.
- Use needs_review only when the supplied record genuinely cannot support a
  confident pass/fail judgment.
- Do not reveal hidden reasoning. Give a concise evidence-based explanation.

Return:
{
  "reviews": [
    {
      "case_ref": "opaque case reference",
      "verdict": "pass|fail|needs_review",
      "total_score": 0-100,
      "dimension_scores": {
        "question_fidelity": 0-20,
        "measurement_quality": 0-20,
        "evidence_quality": 0-20,
        "authority_consistency": 0-15,
        "investigation_quality": 0-15,
        "answer_quality": 0-10
      },
      "critical_failures": ["short code"],
      "explanation": "concise explanation citing supplied artifact fields",
      "confidence": 0.0-1.0
    }
  ]
}
""".strip()


RUBRIC = {
    "question_fidelity": {
        "points": 20,
        "standard": (
            "Preserves metric, population, decision, target month, and "
            "prior-month-end comparison."
        ),
    },
    "measurement_quality": {
        "points": 20,
        "standard": (
            "Chosen windows and exposure are professionally defensible, "
            "explicit, and comparable."
        ),
    },
    "evidence_quality": {
        "points": 20,
        "standard": (
            "Evidence is supported by available rows and respects missingness, "
            "coverage, and claim strength."
        ),
    },
    "authority_consistency": {
        "points": 15,
        "standard": (
            "Frame, query, evidence, and answer retain the same measurement "
            "identity and direction."
        ),
    },
    "investigation_quality": {
        "points": 15,
        "standard": (
            "Material alternatives, sensitivity, data quality, and reversal "
            "conditions are handled in proportion to the decision risk."
        ),
    },
    "answer_quality": {
        "points": 10,
        "standard": (
            "Answer is useful, traceable, bounded, and clear about uncertainty."
        ),
    },
    "critical_failures": [
        "answers a different business question",
        "changes the cross-month comparison into a same-month comparison",
        "uses invented or unsupported evidence",
        "uses incomparable raw totals to establish direction",
        "allows evidence and answer to state opposite directions",
        "publishes settled despite a conclusion-changing unresolved boundary",
    ],
}


WORLD = {
    "user_question": (
        "3 月月初的付费金额和 2 月月末相比表现怎么样？"
        "我想看全部付费用户，不要只看某个渠道。"
    ),
    "as_of": "2025-03-08T10:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "calendar": "Gregorian calendar; business days are Monday-Friday",
    "metric_contract": (
        "payment_amount is successful, non-reversed CNY payment amount; "
        "population is all paying users; observation unit is business date"
    ),
    "snapshot_coverage": "2025-02-01 through 2025-03-07",
    "daily_rows": [
        {"date": "2025-02-22", "weekday": "Sat", "amount_cny": 1250},
        {"date": "2025-02-23", "weekday": "Sun", "amount_cny": 1350},
        {"date": "2025-02-24", "weekday": "Mon", "amount_cny": 1300},
        {"date": "2025-02-25", "weekday": "Tue", "amount_cny": 1200},
        {"date": "2025-02-26", "weekday": "Wed", "amount_cny": 1400},
        {"date": "2025-02-27", "weekday": "Thu", "amount_cny": 1280},
        {"date": "2025-02-28", "weekday": "Fri", "amount_cny": 1320},
        {"date": "2025-03-01", "weekday": "Sat", "amount_cny": 900},
        {"date": "2025-03-02", "weekday": "Sun", "amount_cny": 950},
        {"date": "2025-03-03", "weekday": "Mon", "amount_cny": 1000},
        {"date": "2025-03-04", "weekday": "Tue", "amount_cny": 1050},
        {"date": "2025-03-05", "weekday": "Wed", "amount_cny": 1100},
        {"date": "2025-03-06", "weekday": "Thu", "amount_cny": 950},
        {"date": "2025-03-07", "weekday": "Fri", "amount_cny": 1050},
    ],
}


CASES = [
    {
        "semantic_id": "valid_calendar_7d",
        "expected_verdict": "pass",
        "run_artifacts": {
            "frame": {
                "left": "2025-03-01..2025-03-07, first 7 calendar days of M",
                "right": "2025-02-22..2025-02-28, last 7 calendar days of M-1",
                "exposure": "valid observed calendar day",
                "estimator": "daily average payment amount",
            },
            "agent_rationale": (
                "Both sides contain seven complete natural days and the same "
                "weekday mix. This directly represents opening versus closing."
            ),
            "query_result": {
                "left_total": 7000,
                "left_observed_days": 7,
                "right_total": 9100,
                "right_observed_days": 7,
            },
            "evidence": "1000/day versus 1300/day; change=-23.08%",
            "answer": (
                "settled descriptive answer: March opening daily payment "
                "amount is 23.08% lower; no causal explanation claimed"
            ),
        },
        "derived_fact_sheet": {
            "default_7_calendar_day_reference": (
                "1000/day versus 1300/day; change=-23.08%"
            ),
            "selected_frame_recalculation": (
                "dates and exposure match the 7-day reference"
            ),
            "reference_links": "frame -> query -> evidence -> answer aligned",
        },
    },
    {
        "semantic_id": "valid_business_5d",
        "expected_verdict": "pass",
        "run_artifacts": {
            "frame": {
                "left": "2025-03-03..2025-03-07, first 5 business days of M",
                "right": "2025-02-24..2025-02-28, last 5 business days of M-1",
                "exposure": "valid observed business day",
                "estimator": "daily average payment amount",
            },
            "agent_rationale": (
                "The operating decision concerns staffed payment-operation "
                "days. The business-day contract is available. A seven-calendar-"
                "day sensitivity check preserves the same lower direction."
            ),
            "query_result": {
                "left_total": 5150,
                "left_observed_business_days": 5,
                "right_total": 6500,
                "right_observed_business_days": 5,
            },
            "evidence": (
                "1030/business-day versus 1300/business-day; change=-20.77%; "
                "7-calendar-day sensitivity=-23.08%"
            ),
            "answer": (
                "settled descriptive answer: March opening is lower under the "
                "chosen business-day comparison and the calendar-day sensitivity"
            ),
        },
        "derived_fact_sheet": {
            "default_7_calendar_day_reference": (
                "1000/day versus 1300/day; change=-23.08%"
            ),
            "selected_frame_recalculation": (
                "1030/business-day versus 1300/business-day; change=-20.77%"
            ),
            "pattern_difference": (
                "selected frame uses 5 business days, not the default 7 "
                "calendar-day reference"
            ),
            "reference_links": "frame -> query -> evidence -> answer aligned",
        },
    },
    {
        "semantic_id": "invalid_same_month_drift",
        "expected_verdict": "fail",
        "run_artifacts": {
            "frame": {
                "left": "2025-03-01..2025-03-07",
                "right": "2025-03-08..2025-03-31",
                "exposure": "calendar day",
                "estimator": "daily average payment amount",
            },
            "agent_rationale": "Compares the beginning and remainder of March.",
            "query_result": {
                "left_total": 7000,
                "right_total": 22000,
                "right_observed_days": 24,
            },
            "evidence": "March 1-7 versus March 8-31",
            "answer": "settled answer about March opening versus later March",
        },
        "derived_fact_sheet": {
            "default_7_calendar_day_reference": (
                "requires M month-start versus M-1 month-end"
            ),
            "resolved_anchor_check": (
                "selected right side is month offset 0 and is outside snapshot "
                "coverage; requested right side has month offset -1"
            ),
            "reference_links": "frame identity conflicts with user question",
        },
    },
    {
        "semantic_id": "invalid_raw_total_exposure",
        "expected_verdict": "fail",
        "run_artifacts": {
            "frame": {
                "left": "2025-03-01..2025-03-07, 7 calendar days",
                "right": "2025-02-24..2025-02-28, 5 business days",
                "exposure": "recorded but not used in estimator",
                "estimator": "raw total",
            },
            "agent_rationale": "Uses all available opening days and closing workdays.",
            "query_result": {
                "left_total": 7000,
                "left_observed_days": 7,
                "right_total": 6500,
                "right_observed_days": 5,
            },
            "evidence": "raw total increased 7.69%",
            "answer": "settled descriptive answer: March opening is higher",
        },
        "derived_fact_sheet": {
            "default_7_calendar_day_reference": (
                "1000/day versus 1300/day; change=-23.08%"
            ),
            "selected_frame_recalculation": (
                "1000/observed-day versus 1300/observed-day; normalized "
                "direction is lower while raw-total direction is higher"
            ),
            "reference_links": "answer relies on the incomparable raw-total edge",
        },
    },
    {
        "semantic_id": "invalid_answer_direction",
        "expected_verdict": "fail",
        "run_artifacts": {
            "frame": {
                "left": "2025-03-01..2025-03-07",
                "right": "2025-02-22..2025-02-28",
                "exposure": "valid observed calendar day",
                "estimator": "daily average payment amount",
            },
            "query_result": {
                "left_daily_average": 1000,
                "right_daily_average": 1300,
            },
            "evidence": "March opening is 23.08% lower",
            "answer": "settled answer: March opening is 23.08% higher",
        },
        "derived_fact_sheet": {
            "default_7_calendar_day_reference": (
                "1000/day versus 1300/day; change=-23.08%"
            ),
            "selected_frame_recalculation": "evidence direction is lower",
            "reference_links": "evidence says lower; answer says higher",
        },
    },
    {
        "semantic_id": "valid_missing_day_provisional",
        "expected_verdict": "pass",
        "world_override": (
            "2025-02-27 is unavailable because ingestion is incomplete; all "
            "other listed rows remain available"
        ),
        "run_artifacts": {
            "frame": {
                "left": "2025-03-01..2025-03-07, 7 calendar dates",
                "right": "2025-02-22..2025-02-28, 7 calendar dates",
                "exposure": "valid observed calendar day",
                "estimator": "daily average payment amount",
            },
            "query_result": {
                "left_total": 7000,
                "left_valid_observed_days": 7,
                "right_total": 7820,
                "right_valid_observed_days": 6,
                "missing_date": "2025-02-27",
            },
            "evidence": (
                "provisional comparison: 1000/day versus 1303.33/day; "
                "right-side completeness is 6/7"
            ),
            "answer": (
                "provisional answer: observed data points lower, but the missing "
                "closing day can affect settlement; request repair/release check"
            ),
        },
        "derived_fact_sheet": {
            "default_7_calendar_day_reference": (
                "complete-data reference would be 1000/day versus 1300/day"
            ),
            "selected_frame_recalculation": (
                "1000/day versus 1303.33/valid-day with 6/7 right-side coverage"
            ),
            "pattern_difference": (
                "selected result cannot satisfy the complete 7-day reference"
            ),
            "reference_links": (
                "frame and evidence preserve the missing boundary; answer stays "
                "provisional"
            ),
        },
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/gate3/reviewer-dossier-ab"),
    )
    parser.add_argument("--seed", type=int, default=7302026)
    return parser.parse_args()


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def timeout_seconds() -> float | None:
    raw = os.environ.get("WAJE_LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return None
    value = float(raw)
    return value if value > 0 else None


def reviewer_call(user_payload: dict[str, Any]) -> dict[str, Any]:
    base_url = required_env("WAJE_LLM_BASE_URL").rstrip("/")
    api_key = required_env("WAJE_LLM_API_KEY")
    model = required_env("WAJE_LLM_CRITICAL_MODEL")
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            user_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds(),
            ) as response:
                body = json.loads(response.read())
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            validate_reviewer_result(result, user_payload["cases"])
            return result
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ) as error:
            last_error = error
            if attempt == 3:
                break
            time.sleep(0.5 * attempt)
    raise RuntimeError("reviewer call failed after 3 attempts") from last_error


def validate_reviewer_result(
    result: dict[str, Any],
    packet_cases: list[dict[str, Any]],
) -> None:
    reviews = result.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError("reviewer result lacks reviews")
    expected_refs = {case["case_ref"] for case in packet_cases}
    actual_refs = {review.get("case_ref") for review in reviews}
    if actual_refs != expected_refs:
        raise ValueError("reviewer result case refs do not match packet")
    for review in reviews:
        if review.get("verdict") not in {"pass", "fail", "needs_review"}:
            raise ValueError("reviewer returned invalid verdict")
        score = review.get("total_score")
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            raise ValueError("reviewer returned invalid score")


def build_packet(
    *,
    arm: str,
    repeat: int,
    rng: random.Random,
) -> tuple[dict[str, Any], dict[str, str]]:
    shuffled = list(CASES)
    rng.shuffle(shuffled)
    mapping: dict[str, str] = {}
    packet_cases: list[dict[str, Any]] = []
    for index, case in enumerate(shuffled, start=1):
        case_ref = f"CASE-{repeat + 1}-{index}"
        mapping[case_ref] = case["semantic_id"]
        packet_case = {
            "case_ref": case_ref,
            "world_override": case.get("world_override"),
            "run_artifacts": case["run_artifacts"],
        }
        if arm == "with_derived_dossier":
            packet_case["evaluator_generated_fact_sheet"] = case[
                "derived_fact_sheet"
            ]
        packet_cases.append(packet_case)
    return (
        {
            "rubric": RUBRIC,
            "shared_business_world": WORLD,
            "cases": packet_cases,
        },
        mapping,
    )


def expected_by_semantic_id() -> dict[str, str]:
    return {
        case["semantic_id"]: case["expected_verdict"]
        for case in CASES
    }


def summarize(calls: list[dict[str, Any]]) -> dict[str, Any]:
    expected = expected_by_semantic_id()
    arm_rows: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        for review in call["result"]["reviews"]:
            semantic_id = call["case_mapping"][review["case_ref"]]
            arm_rows.setdefault(call["arm"], []).append(
                {
                    "semantic_id": semantic_id,
                    "expected": expected[semantic_id],
                    "actual": review["verdict"],
                    "score": review["total_score"],
                    "confidence": review.get("confidence"),
                    "critical_failures": review.get("critical_failures", []),
                    "explanation": review.get("explanation", ""),
                }
            )
    arm_summary: dict[str, Any] = {}
    for arm, rows in arm_rows.items():
        correct = sum(row["actual"] == row["expected"] for row in rows)
        valid_rows = [row for row in rows if row["expected"] == "pass"]
        invalid_rows = [row for row in rows if row["expected"] == "fail"]
        arm_summary[arm] = {
            "decisions": len(rows),
            "exact_label_accuracy": round(correct / len(rows), 4),
            "valid_design_acceptance": round(
                sum(row["actual"] == "pass" for row in valid_rows)
                / len(valid_rows),
                4,
            ),
            "critical_failure_recall": round(
                sum(row["actual"] == "fail" for row in invalid_rows)
                / len(invalid_rows),
                4,
            ),
            "mean_score": round(
                statistics.mean(row["score"] for row in rows),
                2,
            ),
            "rows": rows,
        }
    semantic_summary: dict[str, Any] = {}
    for semantic_id in expected:
        semantic_summary[semantic_id] = {}
        for arm, rows in arm_rows.items():
            matching = [
                row for row in rows if row["semantic_id"] == semantic_id
            ]
            semantic_summary[semantic_id][arm] = {
                "verdicts": [row["actual"] for row in matching],
                "mean_score": round(
                    statistics.mean(row["score"] for row in matching),
                    2,
                ),
            }
        without_score = semantic_summary[semantic_id]["raw_artifacts_only"][
            "mean_score"
        ]
        with_score = semantic_summary[semantic_id][
            "with_derived_dossier"
        ]["mean_score"]
        semantic_summary[semantic_id]["dossier_score_delta"] = round(
            with_score - without_score,
            2,
        )
    return {
        "arm_summary": arm_summary,
        "semantic_summary": semantic_summary,
    }


def main() -> int:
    args = parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    rng = random.Random(args.seed)
    scheduled = [
        (arm, repeat)
        for repeat in range(args.repeats)
        for arm in ("raw_artifacts_only", "with_derived_dossier")
    ]
    rng.shuffle(scheduled)
    calls: list[dict[str, Any]] = []
    for arm, repeat in scheduled:
        packet, mapping = build_packet(arm=arm, repeat=repeat, rng=rng)
        result = reviewer_call(packet)
        calls.append(
            {
                "arm": arm,
                "repeat": repeat,
                "case_mapping": mapping,
                "result": result,
            }
        )
        print(f"completed arm={arm} repeat={repeat + 1}", flush=True)
    generated_at = datetime.now(UTC).isoformat()
    artifact = {
        "probe_version": "gate3.reviewer-dossier-ab.v1",
        "generated_at": generated_at,
        "model": required_env("WAJE_LLM_CRITICAL_MODEL"),
        "seed": args.seed,
        "repeats": args.repeats,
        "hypothesis": (
            "A fixed-pattern derived fact sheet may improve obvious error "
            "detection while anchoring the reviewer against valid alternative "
            "measurement designs."
        ),
        "arms": {
            "raw_artifacts_only": (
                "Reviewer receives the business world and Agent run artifacts."
            ),
            "with_derived_dossier": (
                "Reviewer receives the same material plus evaluator-generated "
                "date, exposure, reference-pattern, and link notes."
            ),
        },
        "expected_labels_hidden_from_reviewer": expected_by_semantic_id(),
        "summary": summarize(calls),
        "calls": calls,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    output_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output_path)
    print(
        json.dumps(
            artifact["summary"]["arm_summary"],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
