#!/usr/bin/env python3
"""Compare four DeepSeek V4 profiles for the three WAJE model roles."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from probe_gate3_reviewer_dossier_ab import CASES, WORLD


PROFILES = {
    "flash_without_thinking": {
        "model": "deepseek-v4-flash",
        "thinking": "disabled",
    },
    "flash_with_thinking": {
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
    },
    "pro_without_thinking": {
        "model": "deepseek-v4-pro",
        "thinking": "disabled",
    },
    "pro_with_thinking": {
        "model": "deepseek-v4-pro",
        "thinking": "enabled",
    },
}


PRIMARY_CASES = [
    {
        "case_id": "P-CROSS-MONTH",
        "conversation": [
            "3 月月初的付费金额和 2 月月末相比表现怎么样？",
            "看全部付费用户，不要只看某个渠道。",
        ],
        "world": {
            "as_of": "2025-03-08T10:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "coverage": "2025-02-01 through 2025-03-07 complete",
            "calendar": "natural-day reporting; business-day calendar available",
        },
        "decision": "whether weak March opening warrants investigation",
    },
    {
        "case_id": "P-DRIVER-CHANGE",
        "conversation": [
            "昨天付费金额为什么下降？主要是首充人数、付费频次、"
            "单笔金额还是支付成功率？"
        ],
        "world": {
            "yesterday": "revenue -18%",
            "available_contracts": [
                "successful payment amount",
                "paying users",
                "first payers",
                "payment attempts",
                "successful orders",
                "successful amount",
            ],
            "data_note": (
                "The aggregate movement is known; factor decomposition and "
                "segment concentration have not been queried."
            ),
        },
        "decision": "which operational route to investigate first",
    },
    {
        "case_id": "P-EVENT-CAUSALITY",
        "conversation": [
            "昨天换了素材也更新了版本，收入上涨是不是新素材带来的？"
        ],
        "world": {
            "facts": [
                "creative changed at 11:00",
                "version rollout reached 70% by 18:00",
                "revenue rose during the day",
                "no randomized holdout contract exists",
                "channel and version segment data are available",
            ]
        },
        "decision": "whether to keep or roll back the creative",
    },
    {
        "case_id": "P-INCOMPLETE-RELEASE",
        "conversation": ["昨天收入健康吗？最大的风险点是什么？"],
        "world": {
            "as_of": "today 08:00",
            "release_state": "yesterday is only 72% complete",
            "known_missingness": "late channel and payment reconciliation",
            "available_history": "90 complete days",
        },
        "decision": "whether current revenue needs intervention",
    },
    {
        "case_id": "P-CORRECTION",
        "conversation": [
            "分析昨天全部用户的收入异常。",
            "等等，测试账号和公司内部账号都要排除，之前的结果不要用了。",
        ],
        "world": {
            "current_state": (
                "An accepted frame and two in-flight capability jobs still "
                "include all users."
            ),
            "contract": "test/internal account exclusion contract is available",
        },
        "decision": "identify the genuine-user revenue anomaly",
    },
]


RUNTIME_REVIEW_EXPECTATIONS = {
    "valid_calendar_7d": {
        "publication_decision": "approve_settled",
        "repair_target": None,
    },
    "valid_business_5d": {
        "publication_decision": "approve_settled",
        "repair_target": None,
    },
    "invalid_same_month_drift": {
        "publication_decision": "block_and_repair",
        "repair_target": "frame",
    },
    "invalid_raw_total_exposure": {
        "publication_decision": "block_and_repair",
        "repair_target": "frame",
    },
    "invalid_answer_direction": {
        "publication_decision": "block_and_repair",
        "repair_target": "answer",
    },
    "valid_missing_day_provisional": {
        "publication_decision": "approve_provisional",
        "repair_target": None,
    },
}


EVAL_EXPECTATIONS = {
    case["semantic_id"]: case["expected_verdict"] for case in CASES
}


PRIMARY_SYSTEM = """
You are WAJE's Primary Business Analysis Agent. For each case, propose an
open-ended but typed business investigation. Preserve the user's decision,
make measurement choices explicit, use available facts autonomously, and ask
only for material decisions that cannot be discovered. Do not assume one
canonical measurement window or fixed investigation recipe.

Return only JSON:
{
  "cases": [
    {
      "case_id": "...",
      "disposition": "revise_frame|revise_plan|run_probe|ask_user|propose_provisional",
      "measurement_design": {
        "decision_target": "...",
        "estimand": "...",
        "population": "...",
        "time_or_comparison": "...",
        "observation_unit": "...",
        "numerator": "...",
        "denominator_or_exposure": "...",
        "claim_ceiling": "descriptive|associational|candidate_mechanism|causal"
      },
      "investigation": ["..."],
      "assumptions_or_decisions": ["..."],
      "revision_and_fencing": "...",
      "answer_boundary": "..."
    }
  ]
}
""".strip()


PRIMARY_JUDGE_SYSTEM = """
You are selecting a Primary Business Analysis Agent profile. For every case,
rank four anonymized candidate outputs. Reward fidelity to the user's business
decision, defensible open measurement design, adaptive investigation,
authority/revision discipline, and evidence-bounded claims. Do not require one
canonical window or tool sequence. Penalize unnecessary clarification,
fixed-pattern reasoning, hidden semantic changes, causal overclaiming, and
failure to fence stale work.

Return only JSON:
{
  "cases": [
    {
      "case_id": "...",
      "ranking": ["candidate label best", "...", "...", "candidate label worst"],
      "candidate_scores": {
        "candidate label": {
          "question_and_measurement": 0-3,
          "investigation": 0-3,
          "authority_and_boundaries": 0-3,
          "concise_reason": "..."
        }
      }
    }
  ]
}
""".strip()


RUNTIME_REVIEW_SYSTEM = """
You are WAJE's independent Runtime Reviewer. Review each complete candidate
AnswerVersion once. There is no canonical measurement window. A justified
alternative design can pass. Use only the supplied business world and run
artifacts. Return publication decisions and repair targets; do not score.

Allowed publication_decision:
- approve_provisional
- approve_settled
- block_and_repair
- ask_user
- stop_with_limits

Allowed repair_target: frame, plan, evidence, answer, user, null.

Return only JSON:
{
  "reviews": [
    {
      "case_ref": "...",
      "publication_decision": "...",
      "repair_target": null,
      "claim_reviews": [
        {
          "claim_ref": "main",
          "status": "approve|repair_required|unsupported",
          "objection": "...",
          "evidence_refs": ["..."]
        }
      ],
      "concise_reason": "..."
    }
  ]
}
""".strip()


EVAL_REVIEW_SYSTEM = """
You are WAJE's independent Gate 3 Evaluation Reviewer. Score the completed
episode from raw business context and real run artifacts. There is no canonical
measurement window. Use four anchored levels:
3=professional and complete; 2=acceptable with non-critical gaps;
1=major gap requiring repair or degradation; 0=invalid.

Dimensions:
- question_and_measurement
- investigation
- evidence_and_claims
- authority_consistency
- answer_value

A critical failure forces fail. Use needs_review only when the record cannot
support a confident judgment.

Return only JSON:
{
  "reviews": [
    {
      "case_ref": "...",
      "scores": {
        "question_and_measurement": 0-3,
        "investigation": 0-3,
        "evidence_and_claims": 0-3,
        "authority_consistency": 0-3,
        "answer_value": 0-3
      },
      "critical_failures": ["..."],
      "verdict": "pass|fail|needs_review",
      "concise_reason": "..."
    }
  ]
}
""".strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/gate3/deepseek-role-profile-probe"),
    )
    parser.add_argument("--seed", type=int, default=7312026)
    parser.add_argument("--max-workers", type=int, default=4)
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


def call_profile(
    *,
    profile_name: str,
    system_prompt: str,
    payload: dict[str, Any],
    purpose: str,
) -> dict[str, Any]:
    profile = PROFILES[profile_name]
    body: dict[str, Any] = {
        "model": profile["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": profile["thinking"]},
        "max_tokens": 12000,
    }
    if profile["thinking"] == "disabled":
        body["temperature"] = 0
    request = urllib.request.Request(
        required_env("WAJE_LLM_BASE_URL").rstrip("/") + "/chat/completions",
        data=json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {required_env('WAJE_LLM_API_KEY')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds(),
            ) as response:
                response_body = json.loads(response.read())
            elapsed = time.monotonic() - started
            message = response_body["choices"][0]["message"]
            parsed = json.loads(message["content"])
            return {
                "profile": profile_name,
                "purpose": purpose,
                "elapsed_seconds": round(elapsed, 3),
                "usage": response_body.get("usage", {}),
                "thinking_returned": bool(message.get("reasoning_content")),
                "output": parsed,
            }
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
    raise RuntimeError(
        f"{profile_name}/{purpose} failed after 3 attempts"
    ) from last_error


def parallel_calls(
    jobs: list[dict[str, Any]],
    *,
    max_workers: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    output_lock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:
        future_map = {
            executor.submit(call_profile, **job): job for job in jobs
        }
        for future in concurrent.futures.as_completed(future_map):
            job = future_map[future]
            result = future.result()
            results.append(result)
            with output_lock:
                print(
                    "completed profile={} purpose={} elapsed={:.3f}s".format(
                        job["profile_name"],
                        job["purpose"],
                        result["elapsed_seconds"],
                    ),
                    flush=True,
                )
    return results


def primary_candidate_payloads(
    primary_results: list[dict[str, Any]],
    *,
    rng: random.Random,
) -> tuple[dict[str, Any], dict[str, str]]:
    output_by_profile = {
        result["profile"]: result["output"]["cases"]
        for result in primary_results
    }
    labels = [f"candidate_{letter}" for letter in "WXYZ"]
    shuffled_profiles = list(PROFILES)
    rng.shuffle(shuffled_profiles)
    label_to_profile = dict(zip(labels, shuffled_profiles, strict=True))
    cases: list[dict[str, Any]] = []
    for primary_case in PRIMARY_CASES:
        case_id = primary_case["case_id"]
        candidates: dict[str, Any] = {}
        for label, profile in label_to_profile.items():
            candidate = next(
                item
                for item in output_by_profile[profile]
                if item["case_id"] == case_id
            )
            candidates[label] = candidate
        cases.append(
            {
                "case": primary_case,
                "candidates": candidates,
            }
        )
    return {"cases": cases}, label_to_profile


def runtime_review_payload(
    *, rng: random.Random
) -> tuple[dict[str, Any], dict[str, str]]:
    shuffled = list(CASES)
    rng.shuffle(shuffled)
    mapping: dict[str, str] = {}
    packet_cases: list[dict[str, Any]] = []
    for index, case in enumerate(shuffled, start=1):
        case_ref = f"RUN-{index}"
        mapping[case_ref] = case["semantic_id"]
        packet_cases.append(
            {
                "case_ref": case_ref,
                "world_override": case.get("world_override"),
                "run_artifacts": case["run_artifacts"],
            }
        )
    return (
        {
            "shared_business_world": WORLD,
            "candidate_answer_versions": packet_cases,
        },
        mapping,
    )


def evaluation_payload(
    *, rng: random.Random
) -> tuple[dict[str, Any], dict[str, str]]:
    shuffled = list(CASES)
    rng.shuffle(shuffled)
    mapping: dict[str, str] = {}
    packet_cases: list[dict[str, Any]] = []
    for index, case in enumerate(shuffled, start=1):
        case_ref = f"EVAL-{index}"
        mapping[case_ref] = case["semantic_id"]
        packet_cases.append(
            {
                "case_ref": case_ref,
                "world_override": case.get("world_override"),
                "complete_episode_artifacts": case["run_artifacts"],
            }
        )
    return (
        {
            "shared_business_world": WORLD,
            "completed_episodes": packet_cases,
        },
        mapping,
    )


def summarize_primary(
    primary_results: list[dict[str, Any]],
    judge_results: list[dict[str, Any]],
    label_to_profile: dict[str, str],
) -> dict[str, Any]:
    ranks: dict[str, list[int]] = {profile: [] for profile in PROFILES}
    scores: dict[str, list[float]] = {profile: [] for profile in PROFILES}
    reasons: dict[str, list[str]] = {profile: [] for profile in PROFILES}
    for judge_result in judge_results:
        judge_profile = judge_result["profile"]
        for case in judge_result["output"]["cases"]:
            for rank, label in enumerate(case["ranking"], start=1):
                candidate_profile = label_to_profile[label]
                if candidate_profile != judge_profile:
                    ranks[candidate_profile].append(rank)
            for label, score_record in case["candidate_scores"].items():
                candidate_profile = label_to_profile[label]
                if candidate_profile == judge_profile:
                    continue
                numeric_score = statistics.mean(
                    [
                        score_record["question_and_measurement"],
                        score_record["investigation"],
                        score_record["authority_and_boundaries"],
                    ]
                )
                scores[candidate_profile].append(numeric_score)
                reasons[candidate_profile].append(
                    score_record.get("concise_reason", "")
                )
    latency = {
        result["profile"]: result["elapsed_seconds"]
        for result in primary_results
    }
    return {
        profile: {
            "mean_rank_excluding_self_judge": round(
                statistics.mean(ranks[profile]), 3
            ),
            "mean_dimension_score_excluding_self_judge": round(
                statistics.mean(scores[profile]), 3
            ),
            "generation_latency_seconds": latency[profile],
            "judge_reasons": reasons[profile],
        }
        for profile in PROFILES
    }


def normalize_repair_target(value: Any) -> Any:
    if value in ("", "null", "none", "None"):
        return None
    return value


def summarize_runtime(
    results: list[dict[str, Any]],
    mapping: dict[str, str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for result in results:
        correct_decisions = 0
        correct_repairs = 0
        repairs = 0
        rows: list[dict[str, Any]] = []
        for review in result["output"]["reviews"]:
            semantic_id = mapping[review["case_ref"]]
            expected = RUNTIME_REVIEW_EXPECTATIONS[semantic_id]
            actual_target = normalize_repair_target(
                review.get("repair_target")
            )
            decision_correct = (
                review["publication_decision"]
                == expected["publication_decision"]
            )
            correct_decisions += decision_correct
            repair_correct: bool | None = None
            if expected["repair_target"] is not None:
                repairs += 1
                repair_correct = actual_target == expected["repair_target"]
                correct_repairs += repair_correct
            rows.append(
                {
                    "semantic_id": semantic_id,
                    "expected_decision": expected["publication_decision"],
                    "actual_decision": review["publication_decision"],
                    "expected_repair": expected["repair_target"],
                    "actual_repair": actual_target,
                    "decision_correct": decision_correct,
                    "repair_correct": repair_correct,
                    "reason": review.get("concise_reason", ""),
                }
            )
        summary[result["profile"]] = {
            "publication_decision_accuracy": round(
                correct_decisions / len(rows), 4
            ),
            "repair_target_accuracy": round(
                correct_repairs / repairs, 4
            ),
            "latency_seconds": result["elapsed_seconds"],
            "rows": rows,
        }
    return summary


def summarize_evaluation(
    results: list[dict[str, Any]],
    mapping: dict[str, str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for result in results:
        correct = 0
        valid_accepts = 0
        valid_total = 0
        failure_recalls = 0
        failure_total = 0
        rows: list[dict[str, Any]] = []
        for review in result["output"]["reviews"]:
            semantic_id = mapping[review["case_ref"]]
            expected = EVAL_EXPECTATIONS[semantic_id]
            actual = review["verdict"]
            correct += actual == expected
            if expected == "pass":
                valid_total += 1
                valid_accepts += actual == "pass"
            else:
                failure_total += 1
                failure_recalls += actual == "fail"
            rows.append(
                {
                    "semantic_id": semantic_id,
                    "expected": expected,
                    "actual": actual,
                    "scores": review.get("scores", {}),
                    "critical_failures": review.get(
                        "critical_failures", []
                    ),
                    "reason": review.get("concise_reason", ""),
                }
            )
        summary[result["profile"]] = {
            "verdict_accuracy": round(correct / len(rows), 4),
            "valid_design_acceptance": round(
                valid_accepts / valid_total, 4
            ),
            "failure_recall": round(
                failure_recalls / failure_total, 4
            ),
            "latency_seconds": result["elapsed_seconds"],
            "rows": rows,
        }
    return summary


def main() -> int:
    args = parse_args()
    if args.max_workers < 1:
        raise SystemExit("--max-workers must be positive")
    rng = random.Random(args.seed)

    primary_jobs = [
        {
            "profile_name": profile,
            "system_prompt": PRIMARY_SYSTEM,
            "payload": {"cases": PRIMARY_CASES},
            "purpose": "primary_generation",
        }
        for profile in PROFILES
    ]
    primary_results = parallel_calls(
        primary_jobs,
        max_workers=args.max_workers,
    )

    judge_payload, label_to_profile = primary_candidate_payloads(
        primary_results,
        rng=rng,
    )
    judge_jobs = [
        {
            "profile_name": profile,
            "system_prompt": PRIMARY_JUDGE_SYSTEM,
            "payload": judge_payload,
            "purpose": "primary_blind_judge",
        }
        for profile in PROFILES
    ]
    judge_results = parallel_calls(
        judge_jobs,
        max_workers=args.max_workers,
    )

    runtime_payload, runtime_mapping = runtime_review_payload(rng=rng)
    eval_payload, eval_mapping = evaluation_payload(rng=rng)
    review_jobs = []
    for profile in PROFILES:
        review_jobs.extend(
            [
                {
                    "profile_name": profile,
                    "system_prompt": RUNTIME_REVIEW_SYSTEM,
                    "payload": runtime_payload,
                    "purpose": "runtime_reviewer",
                },
                {
                    "profile_name": profile,
                    "system_prompt": EVAL_REVIEW_SYSTEM,
                    "payload": eval_payload,
                    "purpose": "evaluation_reviewer",
                },
            ]
        )
    review_results = parallel_calls(
        review_jobs,
        max_workers=args.max_workers,
    )
    runtime_results = [
        result
        for result in review_results
        if result["purpose"] == "runtime_reviewer"
    ]
    evaluation_results = [
        result
        for result in review_results
        if result["purpose"] == "evaluation_reviewer"
    ]

    summary = {
        "primary": summarize_primary(
            primary_results,
            judge_results,
            label_to_profile,
        ),
        "runtime_reviewer": summarize_runtime(
            runtime_results,
            runtime_mapping,
        ),
        "evaluation_reviewer": summarize_evaluation(
            evaluation_results,
            eval_mapping,
        ),
    }
    artifact = {
        "probe_version": "gate3.deepseek-role-profile-probe.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "profiles": PROFILES,
        "seed": args.seed,
        "method": {
            "primary": (
                "Five open business cases, blind ranked by all four profiles; "
                "self-judge rows excluded from candidate aggregates."
            ),
            "runtime_reviewer": (
                "Six raw-artifact cases with hidden publication and repair "
                "expectations."
            ),
            "evaluation_reviewer": (
                "The same six cases graded with the approved five-dimension "
                "four-level rubric and hidden verdicts."
            ),
        },
        "blind_label_mapping": label_to_profile,
        "runtime_case_mapping": runtime_mapping,
        "evaluation_case_mapping": eval_mapping,
        "summary": summary,
        "raw_results": {
            "primary": primary_results,
            "primary_judges": judge_results,
            "runtime_reviewers": runtime_results,
            "evaluation_reviewers": evaluation_results,
        },
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
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
