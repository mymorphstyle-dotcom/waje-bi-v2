from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bi_agent.conversation.models import CLARIFICATION_ESCAPE_OPTION  # noqa: E402
from bi_agent.runtime.langgraph_workflow import (  # noqa: E402
    _build_final_route_narrative_payload,
    _business_intent_payload,
    _business_answer_context,
    _business_causal_audit_payload,
    _business_display_review,
    _business_evidence_context,
    _business_final_audit_context,
    _final_business_summary_text,
    _final_narrative_statement_bindings,
    _validate_business_factor_state_narrative,
    _validate_causal_audit_provider_output,
    _validate_business_intent_provider_output,
    _validate_final_answer_audit_provider_output,
)
from bi_agent.runtime.llm_client import (  # noqa: E402
    _chat_completion_request,
    _contains_unlocalized_narrative_tokens,
)
from bi_agent.runtime.llm_prompts import build_prompt  # noqa: E402
from bi_agent.runtime.runtime_contract_registry import (  # noqa: E402
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


DEFAULT_INITIAL_PACKAGE = REPO_ROOT / (
    "artifacts/phase7/human-led-q1/case-b-rerun-11/"
    "human-led-q1-case-b-rerun-11-initial/answer_package.json"
)
DEFAULT_RESUME_PACKAGE = REPO_ROOT / (
    "artifacts/phase7/human-led-q1/case-b-rerun-10/"
    "human-led-q1-case-b-rerun-10-resume-01/answer_package.json"
)
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "artifacts/phase7/human-led-q1"
DEFAULT_FLASH_MODEL = "deepseek-v4-flash"
DEFAULT_PRO_MODEL = "deepseek-v4-pro"
DEFAULT_REPEATS = 5
DEFAULT_WORKERS = 12
DEFAULT_TIMEOUT_SECONDS = 300.0
MODEL_TIERS = ("flash", "pro")
THINKING_MODES = ("enabled", "disabled")
_BUSINESS_INTENT_LEGACY_PLANNING_FIELDS = frozenset(
    {
        "claim_intents",
        "claim_intent_roles",
        "requested_components",
        "requested_dimensions",
        "context_sources",
    }
)
_BUSINESS_INTENT_LOCAL_DERIVATION_FIELDS = frozenset(
    {"required_outcomes", "analysis_axes"}
)

_CASE_B_CURRENT_BUSINESS_DRAFT = (
    "我对问题的理解是：核对2026年6月1日付费金额相较2026年5月31日的变化，"
    "并判断首充人数、付费人数、付费频次、单笔付费金额和支付成功率各自能支持什么结论。\n\n"
    "分析脉络：先确认目标日与基准日的实际方向，再对已完成对账的组成因素进行贡献拆解，"
    "同时把仅观察到变化和缺少独立观测的因素分别保留边界。\n\n"
    "关键发现：2026年6月1日付费金额为308,240,309.0，较2026年5月31日的"
    "304,142,630.0上涨1.35%，增加4,097,679.0。单笔付费金额贡献126.2%，"
    "付费人数贡献2.0%，付费频次贡献-28.2%。首充人数下降但贡献尚未量化；"
    "支付成功率缺少独立观测，本轮按不变处理。\n\n"
    "最终结论：单笔付费金额是主要正向贡献项，付费频次形成负向抵消，付费人数提供小幅正向贡献。"
    "三项会计贡献已经对账，深层业务机制仍需独立证据。\n\n"
    "需要注意：首充人数只能作为当前观察，不能判断影响大小；支付成功率按不变处理不代表实际无影响。"
)


@dataclass(frozen=True)
class ReplayScenario:
    scenario_id: str
    task: str
    provenance: str
    source_path: str
    source_task: str
    source_call_index: int
    source_input_hash: str
    prompt_version: str
    required_keys: tuple[str, ...]
    payload: dict[str, Any]
    messages: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class BenchmarkJob:
    job_id: str
    scenario_id: str
    task: str
    provenance: str
    model_tier: str
    model: str
    thinking: str
    repeat: int
    prompt_version: str
    required_keys: tuple[str, ...]
    input_hash: str
    messages: tuple[dict[str, str], ...]


def load_replay_scenarios(
    *,
    initial_package: Path,
    resume_package: Path,
) -> list[ReplayScenario]:
    initial_path = Path(initial_package)
    resume_path = Path(resume_package)
    initial_calls = _load_root_llm_calls(initial_path)
    resume_calls = _load_root_llm_calls(resume_path)

    scenarios = [
        _business_intent_scenario(
            source_path=initial_path,
            calls=initial_calls,
        ),
        _scenario_from_call(
            scenario_id="boundary_decision_unbound",
            current_task="boundary_decision",
            source_path=initial_path,
            calls=initial_calls,
            source_task="boundary_decision",
        ),
        _scenario_from_call(
            scenario_id="boundary_decision_bound",
            current_task="boundary_decision",
            source_path=resume_path,
            calls=resume_calls,
            source_task="boundary_decision",
        ),
        _scenario_from_call(
            scenario_id="analysis_route_plan",
            current_task="analysis_route_plan",
            source_path=resume_path,
            calls=resume_calls,
            source_task="analysis_route",
            provenance="derived_task_split",
            occurrence=0,
        ),
        _final_route_narrative_scenario(resume_path, resume_calls),
    ]
    for task in (
        "data_coverage_interpretation",
        "next_action",
    ):
        scenarios.append(
            _scenario_from_call(
                scenario_id=task,
                current_task=task,
                source_path=resume_path,
                calls=resume_calls,
                source_task=task,
            )
        )
    scenarios.extend(
        [
            _business_projection_scenario(
                source_path=resume_path,
                calls=resume_calls,
                task="evidence_interpretation",
            ),
            _business_projection_scenario(
                source_path=resume_path,
                calls=resume_calls,
                task="causal_audit",
            ),
            _answer_synthesis_scenario(resume_path, resume_calls),
            _business_projection_scenario(
                source_path=resume_path,
                calls=resume_calls,
                task="semantic_audit",
            ),
            _semantic_audit_unsupported_cause_scenario(
                resume_path,
                resume_calls,
            ),
            _answer_repair_scenario(resume_path, resume_calls),
            _business_projection_scenario(
                source_path=resume_path,
                calls=resume_calls,
                task="final_business_summary",
            ),
            _business_projection_scenario(
                source_path=resume_path,
                calls=resume_calls,
                task="final_answer_audit",
            ),
            _final_answer_audit_variant_scenario(
                resume_path,
                resume_calls,
                scenario_id="final_answer_audit_unsupported_cause",
                added_sentence="已确认某项促销活动导致单笔付费金额上升。",
            ),
            _final_answer_audit_variant_scenario(
                resume_path,
                resume_calls,
                scenario_id="final_answer_audit_payment_success_overclaim",
                added_sentence="支付成功率实际没有影响。",
            ),
        ]
    )
    return scenarios


def build_job_matrix(
    scenarios: Sequence[ReplayScenario],
    *,
    repeats: int = DEFAULT_REPEATS,
    flash_model: str = DEFAULT_FLASH_MODEL,
    pro_model: str = DEFAULT_PRO_MODEL,
    model_tiers: Sequence[str] | None = None,
    thinking_modes: Sequence[str] | None = None,
) -> list[BenchmarkJob]:
    if repeats < 1:
        raise ValueError("repeats_must_be_positive")
    selected_tiers = _validated_selection(
        model_tiers,
        allowed=MODEL_TIERS,
        field="model_tier",
    )
    selected_thinking = _validated_selection(
        thinking_modes,
        allowed=THINKING_MODES,
        field="thinking",
    )
    models = {"flash": flash_model, "pro": pro_model}
    variants = tuple(
        (tier, models[tier], thinking)
        for tier in selected_tiers
        for thinking in selected_thinking
    )
    jobs: list[BenchmarkJob] = []
    for scenario in scenarios:
        input_hash = _hash_json(scenario.messages)
        for model_tier, model, thinking in variants:
            for repeat in range(1, repeats + 1):
                jobs.append(
                    BenchmarkJob(
                        job_id=(
                            f"{scenario.scenario_id}__{model_tier}__"
                            f"thinking_{thinking}__r{repeat:02d}"
                        ),
                        scenario_id=scenario.scenario_id,
                        task=scenario.task,
                        provenance=scenario.provenance,
                        model_tier=model_tier,
                        model=model,
                        thinking=thinking,
                        repeat=repeat,
                        prompt_version=scenario.prompt_version,
                        required_keys=scenario.required_keys,
                        input_hash=input_hash,
                        messages=scenario.messages,
                    )
                )
    return jobs


def execute_job_once(
    job: BenchmarkJob,
    *,
    client: Any,
) -> dict[str, Any]:
    started_at = _utc_now()
    started = perf_counter()
    request = _chat_completion_request(
        model=job.model,
        messages=job.messages,
        thinking=job.thinking,
        deepseek_endpoint=True,
    )
    try:
        response = client.chat.completions.create(**request)
        message = response.choices[0].message
        content = message.content or ""
        parsed, parse_error = _parse_response_object(content)
        validation = _validate_output(job, parsed, parse_error=parse_error)
        usage = _usage_dict(getattr(response, "usage", None))
        return {
            **_job_result_identity(job),
            "status": "completed",
            "started_at": started_at,
            "finished_at": _utc_now(),
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "response_id": str(getattr(response, "id", "") or ""),
            "raw_response_content": content,
            "usage": usage,
            "reasoning_content_present": bool(
                getattr(message, "reasoning_content", None)
            ),
            "validation": validation,
            "output_hash": _sha256_text(content),
            "material_signature": _material_signature(job, parsed),
        }
    except Exception as exc:
        return {
            **_job_result_identity(job),
            "status": "failed",
            "started_at": started_at,
            "finished_at": _utc_now(),
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "error_type": type(exc).__name__,
            "error": _safe_error_text(exc),
            "raw_response_content": "",
            "usage": {},
            "reasoning_content_present": False,
            "validation": {
                "json_object": False,
                "required_keys_pass": False,
                "business_contract_pass": False,
                "errors": ["provider_call_failed"],
            },
            "output_hash": "",
            "material_signature": None,
        }


def reserve_artifact_directory(
    artifact_root: Path,
    *,
    run_token: str | None = None,
) -> Path:
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    token = run_token or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_token = re.sub(r"[^A-Za-z0-9_.-]+", "-", token).strip("-._")
    if not safe_token:
        raise ValueError("invalid_run_token")
    base_name = f"case-b-llm-stability-{safe_token}"
    for number in range(1, 1000):
        suffix = "" if number == 1 else f"-{number:02d}"
        candidate = root / f"{base_name}{suffix}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("artifact_directory_namespace_exhausted")


def run_job_matrix(
    jobs: Sequence[BenchmarkJob],
    *,
    client_factory: Callable[[], Any],
    workers: int = DEFAULT_WORKERS,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    if workers < 1:
        raise ValueError("workers_must_be_positive")
    thread_state = threading.local()

    def run(job: BenchmarkJob) -> dict[str, Any]:
        if not hasattr(thread_state, "client"):
            thread_state.client = client_factory()
        return execute_job_once(job, client=thread_state.client)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run, job): job for job in jobs}
        for completed, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if progress is not None:
                progress(completed, len(jobs))
    return sorted(results, key=lambda item: str(item["job_id"]))


def revalidate_results(
    results: Sequence[Mapping[str, Any]],
    scenarios: Sequence[ReplayScenario],
) -> list[dict[str, Any]]:
    scenarios_by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    revalidated: list[dict[str, Any]] = []
    for raw_result in results:
        result = dict(raw_result)
        scenario_id = str(result.get("scenario_id") or "")
        scenario = scenarios_by_id.get(scenario_id)
        if scenario is None:
            raise ValueError(f"revalidation_scenario_missing:{scenario_id}")
        job = BenchmarkJob(
            job_id=str(result.get("job_id") or ""),
            scenario_id=scenario_id,
            task=scenario.task,
            provenance=scenario.provenance,
            model_tier=str(result.get("model_tier") or ""),
            model=str(result.get("model") or ""),
            thinking=str(result.get("thinking") or ""),
            repeat=int(result.get("repeat") or 0),
            prompt_version=scenario.prompt_version,
            required_keys=scenario.required_keys,
            input_hash=_hash_json(scenario.messages),
            messages=scenario.messages,
        )
        content = str(result.get("raw_response_content") or "")
        parsed, parse_error = _parse_response_object(content)
        result["previous_validation"] = dict(result.get("validation") or {})
        result["validation"] = _validate_output(
            job,
            parsed,
            parse_error=parse_error,
        )
        result["material_signature"] = _material_signature(job, parsed)
        revalidated.append(result)
    return sorted(revalidated, key=lambda item: str(item.get("job_id") or ""))


def write_run_artifacts(
    artifact_directory: Path,
    *,
    scenarios: Sequence[ReplayScenario],
    jobs: Sequence[BenchmarkJob],
    results: Sequence[Mapping[str, Any]],
    run_config: Mapping[str, Any],
) -> dict[str, Path]:
    directory = Path(artifact_directory)
    if not directory.is_dir():
        raise ValueError("artifact_directory_missing")
    scenario_path = directory / "scenarios.json"
    results_path = directory / "results.jsonl"
    manifest_path = directory / "run_manifest.json"
    summary_path = directory / "summary.json"

    _write_json_new(
        scenario_path,
        {
            "scenario_count": len(scenarios),
            "scenarios": [_scenario_record(scenario) for scenario in scenarios],
        },
    )
    with results_path.open("x", encoding="utf-8") as handle:
        for result in results:
            handle.write(_json_line(result) + "\n")
    _write_json_new(
        manifest_path,
        {
            "created_at": _utc_now(),
            "scenario_count": len(scenarios),
            "job_count": len(jobs),
            "run_config": dict(run_config),
        },
    )
    _write_json_new(summary_path, summarize_results(results))
    return {
        "artifact_directory": directory,
        "scenarios": scenario_path,
        "results": results_path,
        "manifest": manifest_path,
        "summary": summary_path,
    }


def summarize_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for result in results:
        key = (
            str(result.get("scenario_id") or ""),
            str(result.get("model_tier") or ""),
            str(result.get("thinking") or ""),
        )
        grouped.setdefault(key, []).append(result)
    groups = []
    for (scenario_id, tier, thinking), items in sorted(grouped.items()):
        completed = [item for item in items if item.get("status") == "completed"]
        schema_passes = [
            item
            for item in items
            if (item.get("validation") or {}).get("required_keys_pass") is True
        ]
        contract_passes = [
            item
            for item in items
            if (item.get("validation") or {}).get("business_contract_pass") is True
        ]
        durations = [float(item.get("duration_ms") or 0) for item in items]
        output_hashes = {
            str(item.get("output_hash") or "")
            for item in completed
            if item.get("output_hash")
        }
        material_signatures = {
            str(item.get("material_signature") or "")
            for item in completed
            if item.get("material_signature")
        }
        usage_total = sum(
            int((item.get("usage") or {}).get("total_tokens") or 0)
            for item in items
        )
        every_result_has_one_material_signature = bool(items) and all(
            bool(item.get("material_signature")) for item in items
        )
        material_signature_stable = (
            every_result_has_one_material_signature
            and len(material_signatures) == 1
        )
        strict_stability_pass = (
            len(completed) == len(items)
            and len(schema_passes) == len(items)
            and len(contract_passes) == len(items)
            and material_signature_stable
        )
        groups.append(
            {
                "scenario_id": scenario_id,
                "model_tier": tier,
                "thinking": thinking,
                "sample_count": len(items),
                "transport_pass_rate": _rate(len(completed), len(items)),
                "required_keys_pass_rate": _rate(len(schema_passes), len(items)),
                "business_contract_pass_rate": _rate(
                    len(contract_passes), len(items)
                ),
                "unique_output_count": len(output_hashes),
                "unique_material_signature_count": len(material_signatures),
                "material_signature_stable": material_signature_stable,
                "strict_stability_pass": strict_stability_pass,
                "latency_ms_p50": _percentile(durations, 0.50),
                "latency_ms_p95": _percentile(durations, 0.95),
                "total_tokens": usage_total,
            }
        )
    total = len(results)
    return {
        "result_count": total,
        "completed_count": sum(
            1 for result in results if result.get("status") == "completed"
        ),
        "required_keys_pass_count": sum(
            1
            for result in results
            if (result.get("validation") or {}).get("required_keys_pass") is True
        ),
        "business_contract_pass_count": sum(
            1
            for result in results
            if (result.get("validation") or {}).get("business_contract_pass") is True
        ),
        "strict_stability_pass": bool(groups)
        and all(group["strict_stability_pass"] for group in groups),
        "groups": groups,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    scenarios = load_replay_scenarios(
        initial_package=args.initial_package,
        resume_package=args.resume_package,
    )
    scenarios = _select_scenarios(scenarios, args.scenario, parser=parser)
    jobs = build_job_matrix(
        scenarios,
        repeats=args.repeats,
        flash_model=args.flash_model,
        pro_model=args.pro_model,
        model_tiers=args.model_tier,
        thinking_modes=args.thinking,
    )
    if args.list_scenarios:
        print(
            json.dumps(
                {
                    "scenario_count": len(scenarios),
                    "scenarios": [_scenario_listing(item) for item in scenarios],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.dry_run:
        print(
            json.dumps(
                {
                    "scenario_count": len(scenarios),
                    "call_count": len(jobs),
                    "repeats": args.repeats,
                    "workers": args.workers,
                    "variants": _job_variants(jobs),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    _load_env_file(args.env_file)
    api_key = (
        os.getenv("WAJE_LLM_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        parser.error("provider API key is missing")
    base_url = os.getenv("WAJE_LLM_BASE_URL", "").strip()
    directory = reserve_artifact_directory(
        args.artifact_root,
        run_token=args.run_token,
    )

    def client_factory():
        from openai import OpenAI

        return OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=args.timeout_seconds,
            max_retries=0,
        )

    last_reported = 0

    def progress(completed: int, total: int) -> None:
        nonlocal last_reported
        if completed == total or completed - last_reported >= max(1, total // 20):
            print(f"completed {completed}/{total}", file=sys.stderr, flush=True)
            last_reported = completed

    results = run_job_matrix(
        jobs,
        client_factory=client_factory,
        workers=args.workers,
        progress=progress,
    )
    paths = write_run_artifacts(
        directory,
        scenarios=scenarios,
        jobs=jobs,
        results=results,
        run_config={
            "repeats": args.repeats,
            "workers": args.workers,
            "flash_model": args.flash_model,
            "pro_model": args.pro_model,
            "model_tiers": list(
                _validated_selection(
                    args.model_tier,
                    allowed=MODEL_TIERS,
                    field="model_tier",
                )
            ),
            "thinking_modes": list(
                _validated_selection(
                    args.thinking,
                    allowed=THINKING_MODES,
                    field="thinking",
                )
            ),
            "timeout_seconds": args.timeout_seconds,
            "provider_retry_count": 0,
            "base_url_hash": _sha256_text(base_url),
        },
    )
    print(
        json.dumps(
            {key: str(value) for key, value in paths.items()},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay high-impact Case B LLM nodes across model/thinking variants."
    )
    parser.add_argument("--initial-package", type=Path, default=DEFAULT_INITIAL_PACKAGE)
    parser.add_argument("--resume-package", type=Path, default=DEFAULT_RESUME_PACKAGE)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--flash-model",
        default=os.getenv("WAJE_LLM_MODEL", DEFAULT_FLASH_MODEL),
    )
    parser.add_argument(
        "--pro-model",
        default=os.getenv("WAJE_LLM_CRITICAL_MODEL", DEFAULT_PRO_MODEL),
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument("--run-token")
    parser.add_argument("--scenario", "--node", dest="scenario", action="append", default=[])
    parser.add_argument(
        "--model-tier",
        action="append",
        choices=MODEL_TIERS,
        default=[],
        help="Limit replay to flash and/or pro. Repeat the option to select both.",
    )
    parser.add_argument(
        "--thinking",
        action="append",
        choices=THINKING_MODES,
        default=[],
        help="Limit replay to enabled and/or disabled thinking mode.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-scenarios", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    return parser


def _load_root_llm_calls(path: Path) -> list[dict[str, Any]]:
    package = json.loads(path.read_text(encoding="utf-8"))
    calls = package.get("llm_calls")
    if not isinstance(calls, list):
        calls = (package.get("admin_audit") or {}).get("llm_calls")
    if not isinstance(calls, list):
        raise ValueError(f"replay_llm_calls_missing:{path}")
    return [dict(item) for item in calls if isinstance(item, Mapping)]


def _scenario_from_call(
    *,
    scenario_id: str,
    current_task: str,
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
    source_task: str,
    occurrence: int = 0,
    provenance: str = "exact_replay_payload",
) -> ReplayScenario:
    call_index, call = _find_call(calls, source_task, occurrence)
    payload = _extract_input_payload(call.get("messages") or ())
    spec = build_prompt(current_task, payload)
    return ReplayScenario(
        scenario_id=scenario_id,
        task=current_task,
        provenance=provenance,
        source_path=str(source_path),
        source_task=source_task,
        source_call_index=call_index,
        source_input_hash=str(call.get("input_hash") or _hash_json(call.get("messages"))),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _business_intent_scenario(
    *,
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
) -> ReplayScenario:
    call_index, call = _find_call(calls, "business_intent", 0)
    replay_payload = _extract_input_payload(call.get("messages") or ())
    request = _business_intent_request_from_replay_payload(replay_payload)
    payload = _business_intent_payload(request)
    spec = build_prompt("business_intent", payload)
    return ReplayScenario(
        scenario_id="business_intent",
        task="business_intent",
        provenance="exact_question_current_contract_projection",
        source_path=str(source_path),
        source_task="business_intent",
        source_call_index=call_index,
        source_input_hash=str(
            call.get("input_hash") or _hash_json(call.get("messages"))
        ),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _business_intent_request_from_replay_payload(
    replay_payload: Mapping[str, Any],
) -> dict[str, Any]:
    question = str(replay_payload.get("question") or "").strip()
    if not question:
        raise ValueError("business_intent_replay_question_missing")
    request: dict[str, Any] = {"question": question}
    bound = replay_payload.get("bound_business_context")
    if isinstance(bound, Mapping):
        for key in (
            "target_metric",
            "pattern_family",
            "pattern_params",
            "scope",
            "time_window",
            "baseline",
            "target",
        ):
            if key in bound:
                request[key] = bound[key]
    reviewed_window = replay_payload.get("reviewed_time_window_recommendation")
    if isinstance(reviewed_window, Mapping):
        time_window = reviewed_window.get("time_window")
        if isinstance(time_window, str) and time_window:
            request["analysis_context"] = {"target_date": time_window}
    return request


def _final_route_narrative_scenario(
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
) -> ReplayScenario:
    call_index, call = _find_call(calls, "analysis_route", 1)
    source_payload = _extract_input_payload(call.get("messages") or ())
    route = source_payload.get("final_route_machine")
    if not isinstance(route, Mapping):
        raise ValueError("final_route_machine_missing")
    requested = route.get("requested_nodes")
    if not isinstance(requested, (list, tuple)) or not requested:
        raise ValueError("final_route_requested_nodes_missing")
    payload, _ = _build_final_route_narrative_payload(
        {"intent": source_payload.get("intent") or {}},
        requested=[str(item) for item in requested],
        route=route,
    )
    spec = build_prompt("final_route_narrative", payload)
    return ReplayScenario(
        scenario_id="final_route_narrative",
        task="final_route_narrative",
        provenance="derived_task_split",
        source_path=str(source_path),
        source_task="analysis_route",
        source_call_index=call_index,
        source_input_hash=str(call.get("input_hash") or _hash_json(call.get("messages"))),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _answer_synthesis_scenario(
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
) -> ReplayScenario:
    call_index, call = _find_call(calls, "answer_synthesis", 0)
    source_payload = _extract_input_payload(call.get("messages") or ())
    payload = {"businessContext": _business_answer_context(source_payload)}
    spec = build_prompt("answer_synthesis", payload)
    return ReplayScenario(
        scenario_id="answer_synthesis",
        task="answer_synthesis",
        provenance="derived_business_projection",
        source_path=str(source_path),
        source_task="answer_synthesis",
        source_call_index=call_index,
        source_input_hash=str(
            call.get("input_hash") or _hash_json(call.get("messages"))
        ),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _business_projection_scenario(
    *,
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
    task: str,
) -> ReplayScenario:
    call_index, call = _find_call(calls, task, 0)
    state = _business_replay_state(calls, stage=task)
    if task == "evidence_interpretation":
        payload = {"businessContext": _business_evidence_context(state)}
    elif task == "causal_audit":
        payload = _business_causal_audit_payload(state)
    elif task == "semantic_audit":
        payload = {
            "answerText": state.get("answer_text", ""),
            "businessContext": _business_answer_context(state),
            "displayReview": _business_display_review(state, stage="semantic"),
        }
    elif task == "final_business_summary":
        payload = {
            "draftAnswer": state.get("answer_text", ""),
            "businessContext": _business_answer_context(state),
            "displayReview": _business_display_review(state, stage="final"),
        }
    elif task == "final_answer_audit":
        payload = {
            "finalAnswer": state.get("final_business_summary", ""),
            "businessContext": _business_final_audit_context(state),
            "displayReview": _business_display_review(state, stage="final"),
        }
    else:
        raise ValueError(f"unsupported_business_projection_task:{task}")
    spec = build_prompt(task, payload)
    return ReplayScenario(
        scenario_id=task,
        task=task,
        provenance="derived_business_projection",
        source_path=str(source_path),
        source_task=task,
        source_call_index=call_index,
        source_input_hash=str(
            call.get("input_hash") or _hash_json(call.get("messages"))
        ),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _answer_repair_scenario(
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
) -> ReplayScenario:
    call_index, call = _find_call(calls, "semantic_audit", 0)
    state = _business_replay_state(calls, stage="answer_repair")
    state["answer_text"] = (
        _CASE_B_CURRENT_BUSINESS_DRAFT
        + "\n\n已确认某项促销活动导致单笔付费金额上升。"
    )
    state["semantic_audit"] = {
        "audit_status": "needs_revision",
        "issues": [
            {
                "severity": "error",
                "description": "当前答案增加了未经证据验证的深层原因。",
            }
        ],
    }
    payload = {
        "answerText": state["answer_text"],
        "businessContext": _business_answer_context(state),
        "displayReview": _business_display_review(state, stage="semantic"),
    }
    spec = build_prompt("answer_repair", payload)
    return ReplayScenario(
        scenario_id="answer_repair",
        task="answer_repair",
        provenance="derived_business_projection",
        source_path=str(source_path),
        source_task="semantic_audit",
        source_call_index=call_index,
        source_input_hash=str(
            call.get("input_hash") or _hash_json(call.get("messages"))
        ),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _semantic_audit_unsupported_cause_scenario(
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
) -> ReplayScenario:
    call_index, call = _find_call(calls, "semantic_audit", 0)
    state = _business_replay_state(calls, stage="semantic_audit")
    state["answer_text"] = (
        _CASE_B_CURRENT_BUSINESS_DRAFT
        + "\n\n已确认某项促销活动导致单笔付费金额上升。"
    )
    payload = {
        "answerText": state["answer_text"],
        "businessContext": _business_answer_context(state),
        "displayReview": _business_display_review(state, stage="semantic"),
    }
    spec = build_prompt("semantic_audit", payload)
    return ReplayScenario(
        scenario_id="semantic_audit_unsupported_cause",
        task="semantic_audit",
        provenance="derived_business_projection",
        source_path=str(source_path),
        source_task="semantic_audit",
        source_call_index=call_index,
        source_input_hash=str(
            call.get("input_hash") or _hash_json(call.get("messages"))
        ),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _final_answer_audit_variant_scenario(
    source_path: Path,
    calls: Sequence[Mapping[str, Any]],
    *,
    scenario_id: str,
    added_sentence: str,
) -> ReplayScenario:
    call_index, call = _find_call(calls, "final_answer_audit", 0)
    state = _business_replay_state(calls, stage="final_answer_audit")
    final_answer = _CASE_B_CURRENT_BUSINESS_DRAFT + "\n\n" + added_sentence
    payload = {
        "finalAnswer": final_answer,
        "businessContext": _business_final_audit_context(state),
        "displayReview": _business_display_review(state, stage="final"),
    }
    spec = build_prompt("final_answer_audit", payload)
    return ReplayScenario(
        scenario_id=scenario_id,
        task="final_answer_audit",
        provenance="derived_business_projection",
        source_path=str(source_path),
        source_task="final_answer_audit",
        source_call_index=call_index,
        source_input_hash=str(
            call.get("input_hash") or _hash_json(call.get("messages"))
        ),
        prompt_version=spec.prompt_version,
        required_keys=spec.required_keys,
        payload=payload,
        messages=spec.messages,
    )


def _business_replay_state(
    calls: Sequence[Mapping[str, Any]],
    *,
    stage: str,
) -> dict[str, Any]:
    _, answer_call = _find_call(calls, "answer_synthesis", 0)
    _, semantic_call = _find_call(calls, "semantic_audit", 0)
    _, summary_call = _find_call(calls, "final_business_summary", 0)
    _, final_audit_call = _find_call(calls, "final_answer_audit", 0)
    answer_input = _extract_input_payload(answer_call.get("messages") or ())
    semantic_input = _extract_input_payload(semantic_call.get("messages") or ())
    summary_input = _extract_input_payload(summary_call.get("messages") or ())
    final_audit_input = _extract_input_payload(
        final_audit_call.get("messages") or ()
    )
    state = {
        **answer_input,
        "request": {
            "run_mode": "production",
            "question": final_audit_input.get("user_question", ""),
        },
        "analysis_route": {
            "claim_intent_resolution": summary_input.get(
                "claim_intent_resolution", {}
            )
        },
        "draft_claims": semantic_input.get("draft_claims") or (),
        "answer_text": _CASE_B_CURRENT_BUSINESS_DRAFT,
        "semantic_audit": summary_input.get("semantic_audit")
        or final_audit_input.get("semantic_audit")
        or {},
        "verifier": summary_input.get("verifier")
        or final_audit_input.get("verifier")
        or {},
        "final_summary_display_warnings": final_audit_input.get(
            "final_summary_display_warnings", ()
        ),
        "final_business_summary": _CASE_B_CURRENT_BUSINESS_DRAFT,
        "validator_results": (),
    }
    if stage in {"final_business_summary", "final_answer_audit"}:
        state["authority_verified_claims"] = (
            final_audit_input.get("verified_claims")
            or summary_input.get("claims")
            or ()
        )
    return state


def _find_call(
    calls: Sequence[Mapping[str, Any]],
    task: str,
    occurrence: int,
) -> tuple[int, Mapping[str, Any]]:
    matches = [
        (index, call)
        for index, call in enumerate(calls)
        if str(call.get("task") or "") == task
    ]
    try:
        return matches[occurrence]
    except IndexError as exc:
        raise ValueError(f"replay_task_missing:{task}:{occurrence}") from exc


def _extract_input_payload(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    user_messages = [
        str(message.get("content") or "")
        for message in messages
        if str(message.get("role") or "") == "user"
    ]
    if not user_messages:
        raise ValueError("replay_user_message_missing")
    match = re.search(
        r"<input_json>\s*(.*?)\s*</input_json>",
        user_messages[-1],
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("replay_input_json_missing")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError("replay_input_json_not_object")
    return payload


def _validate_output(
    job: BenchmarkJob,
    parsed: dict[str, Any] | None,
    *,
    parse_error: str | None,
) -> dict[str, Any]:
    errors: list[str] = []
    if parse_error:
        errors.append(parse_error)
    missing = [
        key for key in job.required_keys if parsed is None or key not in parsed
    ]
    if missing:
        errors.append("missing_required_keys:" + ",".join(missing))
    contract_errors = _task_contract_errors(job, parsed)
    errors.extend(contract_errors)
    return {
        "json_object": parsed is not None,
        "required_keys_pass": parsed is not None and not missing,
        "business_contract_pass": parsed is not None and not errors,
        "missing_required_keys": missing,
        "errors": errors,
    }


def _task_contract_errors(
    job: BenchmarkJob,
    parsed: Mapping[str, Any] | None,
) -> list[str]:
    if parsed is None:
        return []
    errors: list[str] = []
    if job.task == "analysis_route_plan":
        if not isinstance(parsed.get("requested_nodes"), list):
            errors.append("requested_nodes_not_array")
        if not isinstance(parsed.get("analysis_requirements"), Mapping):
            errors.append("analysis_requirements_not_object")
        forbidden = {
            "route_summary",
            "sections",
            "capability_sections",
            "decision_summary",
            "display_summary",
        }
        leaked = sorted(forbidden.intersection(parsed))
        if leaked:
            errors.append("machine_plan_contains_narrative:" + ",".join(leaked))
    elif job.task == "final_route_narrative":
        input_payload = _extract_input_payload(job.messages)
        steps = (input_payload.get("route_context") or {}).get("route_steps") or []
        expected_refs = [str(step.get("step_ref") or "") for step in steps]
        sections = parsed.get("sections")
        actual_refs = (
            [str(section.get("step_ref") or "") for section in sections]
            if isinstance(sections, list)
            and all(isinstance(section, Mapping) for section in sections)
            else []
        )
        if actual_refs != expected_refs:
            errors.append("route_step_refs_changed")
        prose = _narrative_values(parsed)
        if any(ref and ref in prose for ref in expected_refs):
            errors.append("step_ref_leaked_into_narrative")
        if re.search(r"(?<![A-Za-z0-9])[a-z][a-z0-9]*_[a-z0-9_]+", prose):
            errors.append("machine_identifier_in_narrative")
    elif job.task == "business_intent":
        input_payload = _extract_input_payload(job.messages)
        legacy_fields = _nested_mapping_keys(
            parsed,
            _BUSINESS_INTENT_LEGACY_PLANNING_FIELDS,
        )
        if legacy_fields:
            errors.append("business_intent_legacy_planning_field_present")
        provider_derived_fields = _nested_mapping_keys(
            parsed,
            _BUSINESS_INTENT_LOCAL_DERIVATION_FIELDS,
        )
        if provider_derived_fields:
            errors.append("business_intent_local_derivation_owned_by_provider")
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
        request = _business_intent_request_from_replay_payload(input_payload)
        try:
            _validate_business_intent_provider_output(
                parsed,
                request,
                registry,
            )
        except Exception as exc:
            errors.append(str(exc))
        closed_fields = {
            "target_metric": "allowed_target_metric_ids",
            "scope": "allowed_scope_types",
        }
        for output_field, allowed_field in closed_fields.items():
            allowed = set(input_payload.get(allowed_field) or ())
            value = parsed.get(output_field)
            if allowed and value not in allowed:
                errors.append(f"closed_world_violation:{output_field}")
        candidates = parsed.get("baseline_candidates")
        if not isinstance(candidates, list):
            errors.append("baseline_candidates_not_array")
        elif not set(candidates).issubset(
            set(input_payload.get("allowed_baseline_ids") or ())
        ):
            errors.append("closed_world_violation:baseline_candidates")
    elif job.task == "boundary_decision":
        status = parsed.get("boundary_status")
        if status not in {
            "clear",
            "low_risk_assumption",
            "needs_question",
            "cannot_answer",
        }:
            errors.append("boundary_status_invalid")
        questions = parsed.get("clarification_questions")
        if status == "needs_question" and not questions:
            errors.append("needs_question_without_question")
        if status != "needs_question" and questions:
            errors.append("question_present_for_nonblocking_status")
    elif job.task == "answer_synthesis" and "claims" in parsed:
        errors.append("answer_synthesis_returned_canonical_claims")
    elif job.task == "semantic_audit" and "extracted_claims" in parsed:
        errors.append("semantic_audit_returned_provider_claims")
    elif job.task == "final_business_summary":
        try:
            _final_business_summary_text(parsed)
            _final_narrative_statement_bindings(parsed)
        except Exception as exc:
            errors.append(str(exc))
        display_summary = parsed.get("display_summary")
        if (
            not isinstance(display_summary, str)
            or not display_summary.strip()
            or display_summary != display_summary.strip()
        ):
            errors.append("final_business_summary_display_summary_invalid")
    elif job.task == "causal_audit":
        input_payload = _extract_input_payload(job.messages)
        try:
            _validate_causal_audit_provider_output(parsed, input_payload)
        except Exception as exc:
            errors.append(str(exc))
    elif job.task == "final_answer_audit":
        input_payload = _extract_input_payload(job.messages)
        try:
            _validate_final_answer_audit_provider_output(
                parsed,
                final_answer=str(input_payload.get("finalAnswer") or ""),
                business_context=input_payload.get("businessContext") or {},
            )
        except Exception as exc:
            errors.append(str(exc))
    errors.extend(_case_b_expectation_errors(job, parsed))
    return errors


def _case_b_expectation_errors(
    job: BenchmarkJob,
    output: Mapping[str, Any],
) -> list[str]:
    payload = _extract_input_payload(job.messages)
    scenario = job.scenario_id
    errors: list[str] = []
    prose = _output_prose(output)

    if scenario == "business_intent":
        expected_values = {
            "question_family": "paid_amount_change_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-06-01",
        }
        for field, expected in expected_values.items():
            if output.get(field) != expected:
                errors.append(f"case_b_value_mismatch:{field}")
        requirements = output.get("analysis_requirements") or {}
        expected_requirements = {
            "goal_bindings": [
                {"goal_id": "explain_change", "role": "primary"}
            ],
            "explicit_focus": {
                "component_ids": [],
                "dimension_ids": [],
                "context_source_ids": [],
            },
        }
        if requirements != expected_requirements:
            errors.append("case_b_analysis_goal_binding_mismatch")
        if _prequery_direction_asserted(prose):
            errors.append("case_b_direction_confirmed_before_query")
        if re.search(
            r"基线(?:已)?(?:确认为|确定为|为)前一(?:天|日)",
            prose,
        ) and not any(
            token in prose for token in ("候选", "建议", "待确认", "未确认")
        ):
            errors.append("case_b_unbound_baseline_worded_as_selected")

    elif scenario == "boundary_decision_unbound":
        if output.get("boundary_status") != "needs_question":
            errors.append("case_b_unbound_baseline_not_clarified")
        questions = output.get("clarification_questions")
        if not isinstance(questions, list) or len(questions) != 1:
            errors.append("case_b_unbound_baseline_question_count")
        if not any(token in prose for token in ("前一天", "前一日")):
            errors.append("case_b_previous_day_not_recommended")

    elif scenario == "boundary_decision_bound":
        if output.get("boundary_status") not in {
            "clear",
            "low_risk_assumption",
        }:
            errors.append("case_b_bound_baseline_not_clear")
        if output.get("clarification_questions") != []:
            errors.append("case_b_bound_baseline_reasked")

    elif scenario == "analysis_route_plan":
        requested = output.get("requested_nodes") or ()
        required = set(payload.get("required_capability_ids") or ())
        allowed = {
            str(card.get("capability_id") or "")
            for card in payload.get("known_capabilities") or ()
            if isinstance(card, Mapping)
        }
        if not required.issubset(set(requested)):
            errors.append("case_b_required_capability_missing")
        if not set(requested).issubset(allowed):
            errors.append("case_b_unknown_capability_selected")
        requirements = output.get("analysis_requirements") or {}
        if requirements.get("target_metrics") != ["paid_amount"]:
            errors.append("case_b_target_metric_requirement_changed")
        if requirements.get("baselines") != ["previous_day"]:
            errors.append("case_b_baseline_requirement_changed")
        if requirements.get("scope") != "full_sample":
            errors.append("case_b_scope_requirement_changed")
        claims = set(requirements.get("claim_intents") or ())
        if not {
            "comparative_change",
            "formula_component_contribution",
        }.issubset(claims):
            errors.append("case_b_route_required_claim_missing")

    elif scenario == "final_route_narrative":
        if _prequery_direction_asserted(prose):
            errors.append("case_b_direction_confirmed_before_query")
        if not (
            any(token in prose for token in ("验证", "核对"))
            and any(token in prose for token in ("方向", "上涨", "假设"))
        ):
            errors.append("case_b_direction_verification_missing")
        if _global_core_block_wording(prose):
            errors.append("case_b_auxiliary_gap_blocks_core")

    elif scenario == "data_coverage_interpretation":
        if output.get("coverage_status") in {"blocked", "needs_question"}:
            errors.append("case_b_complete_queries_blocked")
        if _global_core_block_wording(prose):
            errors.append("case_b_coverage_gap_globalized")
        target_covered = any(token in prose for token in ("6月1日", "2026-06-01"))
        baseline_covered = any(
            token in prose
            for token in ("5月31日", "2026-05-31", "前一天", "前一日")
        )
        if not (target_covered and baseline_covered):
            errors.append("case_b_primary_comparison_coverage_omitted")

    elif scenario == "next_action":
        if output.get("next_action") != "synthesize_answer":
            errors.append("case_b_publishable_core_not_synthesized")

    elif scenario == "evidence_interpretation":
        consumed_prose = "\n".join(
            str(output.get(field) or "")
            for field in (
                "interpretation",
                "decision_summary",
                "evidence_boundary",
            )
        )
        errors.extend(_core_factor_text_errors(consumed_prose))
        try:
            _validate_business_factor_state_narrative(
                output,
                payload,
                fields=(
                    "interpretation",
                    "decision_summary",
                    "evidence_boundary",
                ),
            )
        except Exception:
            errors.append("case_b_factor_state_narrative_conflict")
        errors.extend(
            _factor_state_narrative_errors(
                consumed_prose,
                payload.get("businessContext") or {},
            )
        )

    elif scenario == "causal_audit":
        if output.get("causal_assessment") != "not_supported":
            errors.append("case_b_causal_assessment_changed")
        publishable = str(output.get("publishable_wording") or "")
        for factor, code in (
            ("单笔付费金额", "case_b_primary_core_driver_missing"),
            ("付费频次", "case_b_frequency_factor_missing"),
            ("付费人数", "case_b_paid_user_factor_missing"),
        ):
            if factor not in publishable:
                errors.append(code)
        if not any(
            marker in publishable
            for marker in (
                "会计贡献",
                "会计拆解",
                "会计层面",
                "组成贡献",
                "贡献分解",
                "对账",
            )
        ):
            errors.append("case_b_accounting_boundary_missing")
        if not re.search(
            r"(?:缺乏|缺少|尚未|没有|无).{0,12}(?:独立)?(?:因果|机制).{0,12}(?:验证|证据|确认)"
            r"|(?:因果|机制).{0,12}(?:未验证|未确认|证据不足|不支持|尚未建立)"
            r"|(?:因果|机制).{0,12}(?:缺乏|缺少|尚无|没有|无).{0,8}(?:独立)?(?:证据|验证)",
            prose,
        ):
            errors.append("case_b_causal_boundary_missing")

    elif scenario == "answer_synthesis":
        errors.extend(_core_factor_text_errors(str(output.get("answer_text") or "")))

    elif scenario == "semantic_audit":
        if output.get("audit_status") == "fail":
            errors.append("case_b_publishable_claims_hard_failed")
        issues = output.get("issues") or ()
        blocking_issues = [
            issue
            for issue in issues
            if isinstance(issue, Mapping)
            and str(issue.get("severity") or "").lower()
            in {"error", "critical", "blocking"}
        ]
        if output.get("audit_status") == "needs_revision" and (
            blocking_issues
            or not all(
                isinstance(issue, Mapping)
                and str(issue.get("severity") or "").lower()
                in {"info", "warning"}
                for issue in issues
            )
        ):
            errors.append("case_b_supported_draft_unnecessarily_repaired")
        if output.get("audit_status") == "passed" and any(
            isinstance(issue, Mapping)
            and str(issue.get("severity") or "") in {"error", "critical"}
            for issue in issues
        ):
            errors.append("case_b_audit_status_severity_conflict")

    elif scenario == "semantic_audit_unsupported_cause":
        issues = [
            issue
            for issue in output.get("issues") or ()
            if isinstance(issue, Mapping)
        ]
        if output.get("audit_status") not in {"needs_revision", "fail"}:
            errors.append("case_b_unsupported_cause_not_rejected")
        if not any(
            str(issue.get("severity") or "").lower()
            in {"error", "critical", "blocking"}
            for issue in issues
        ):
            errors.append("case_b_unsupported_cause_not_material")
        issue_text = "\n".join(
            str(issue.get("description") or "")
            for issue in issues
        )
        if (
            any(
                marker in issue_text
                for marker in (
                    "初步推测",
                    "外部信息指出",
                    "假设性表述",
                )
            )
            or re.search(
                r"(?:建议|改为|修改为).{0,100}促销活动.{0,30}"
                r"(?:可能|相关|关系|影响)",
                issue_text,
            )
        ):
            errors.append("case_b_unsupported_cause_recast_as_hypothesis")

    elif scenario == "answer_repair":
        repaired_answer = str(output.get("answer_text") or "")
        errors.extend(_core_factor_text_errors(repaired_answer))
        if re.search(r"已确认.{0,20}促销.{0,20}导致", repaired_answer):
            errors.append("case_b_unsupported_mechanism_not_removed")

    elif scenario == "final_business_summary":
        summary = str(output.get("summary_text") or "")
        required_labels = (
            "我对问题的理解是：",
            "分析脉络：",
            "关键发现：",
            "最终结论：",
            "需要注意：",
        )
        if any(summary.count(label) != 1 for label in required_labels):
            errors.append("case_b_final_summary_shape_changed")
        errors.extend(_core_factor_text_errors(summary))

    elif scenario == "final_answer_audit":
        if output.get("material_findings") != []:
            errors.append("case_b_supported_final_answer_false_finding")

    elif scenario == "final_answer_audit_unsupported_cause":
        findings = [
            finding
            for finding in output.get("material_findings") or ()
            if isinstance(finding, Mapping)
        ]
        if not any(
            finding.get("code") == "unsupported_material_claim"
            and "促销活动" in str(finding.get("answer_excerpt") or "")
            and isinstance(finding.get("context_anchor"), Mapping)
            and finding["context_anchor"].get("kind") == "boundary"
            for finding in findings
        ):
            errors.append("case_b_unsupported_cause_finding_missing")

    elif scenario == "final_answer_audit_payment_success_overclaim":
        findings = [
            finding
            for finding in output.get("material_findings") or ()
            if isinstance(finding, Mapping)
        ]
        if not any(
            finding.get("code") == "unsupported_material_claim"
            and "支付成功率" in str(finding.get("answer_excerpt") or "")
            and "没有影响" in str(finding.get("answer_excerpt") or "")
            and isinstance(finding.get("context_anchor"), Mapping)
            and finding["context_anchor"].get("kind") == "factor_state"
            and finding["context_anchor"].get("key") == "支付成功率"
            for finding in findings
        ):
            errors.append("case_b_payment_success_overclaim_finding_missing")
    if scenario in {
        "evidence_interpretation",
        "answer_synthesis",
        "answer_repair",
        "final_business_summary",
    }:
        if not any(token in prose for token in ("2026-05-31", "5月31日")):
            errors.append("case_b_baseline_date_missing")
        if re.search(
            r"(?:基准日|基线日|基准窗口|基线窗口).{0,10}2026-06-01",
            prose,
        ):
            errors.append("case_b_baseline_target_date_confused")
    narrative = _case_b_business_narrative(scenario, output)
    if re.search(
        r"(?<![A-Za-z0-9])[a-z][a-z0-9]*_[a-z0-9_]+",
        narrative,
    ):
        errors.append("case_b_business_narrative_internal_identifier")
    if narrative and _contains_unlocalized_narrative_tokens(narrative):
        errors.append("case_b_business_narrative_unlocalized_token")
    if re.search(r"(?:结论|声明)\s*[0-9]+", narrative):
        errors.append("case_b_business_narrative_claim_slot_label")
    if scenario.startswith("final_answer_audit") and re.search(
        r"(?:上下文锚点|证据锚点|材料性|展示检查|表达质量审核|审计通过|审核通过)",
        narrative,
    ):
        errors.append("case_b_final_audit_process_jargon")
    if re.search(
        r"单笔付费金额.{0,8}(?:ARPU|ARPPU)",
        narrative,
        re.IGNORECASE,
    ):
        errors.append("case_b_avg_order_amount_mislabeled")
    return errors


def _case_b_business_narrative(
    scenario: str,
    output: Mapping[str, Any],
) -> str:
    if scenario.startswith("final_answer_audit"):
        fields = ()
    elif scenario.startswith("semantic_audit"):
        fields = ()
    else:
        fields = {
            "business_intent": (
                "target_claim",
                "status_message",
                "display_summary",
            ),
            "boundary_decision_unbound": (
                "clarification_questions",
                "decision_summary",
                "display_summary",
            ),
            "boundary_decision_bound": (
                "decision_summary",
                "display_summary",
            ),
            "final_route_narrative": (
                "route_summary",
                "decision_summary",
                "display_summary",
            ),
            "data_coverage_interpretation": (
                "business_impact",
                "decision_summary",
                "display_summary",
            ),
            "next_action": ("decision_summary", "display_summary"),
            "evidence_interpretation": (
                "interpretation",
                "decision_summary",
                "evidence_boundary",
            ),
            "causal_audit": (
                "publishable_wording",
                "supporting_reasons",
                "evidence_limit",
                "display_summary",
            ),
            "answer_synthesis": ("answer_text", "display_summary"),
            "answer_repair": ("answer_text", "display_summary"),
            "final_business_summary": ("summary_text", "display_summary"),
        }.get(scenario, ())
    values: list[str] = []
    for field in fields:
        field_values = _nested_text_values(output.get(field))
        if field == "clarification_questions":
            field_values = [
                value
                for value in field_values
                if value.strip() != CLARIFICATION_ESCAPE_OPTION
            ]
        values.extend(field_values)
    if scenario == "final_route_narrative":
        for section in output.get("sections") or ():
            if not isinstance(section, Mapping):
                continue
            values.extend(_nested_text_values(section.get("route_step")))
            values.extend(_nested_text_values(section.get("expected_evidence")))
    if scenario.startswith("final_answer_audit"):
        for finding in output.get("material_findings") or ():
            if not isinstance(finding, Mapping):
                continue
            values.extend(_nested_text_values(finding.get("answer_excerpt")))
            values.extend(_nested_text_values(finding.get("explanation")))
    if scenario.startswith("semantic_audit"):
        for issue in output.get("issues") or ():
            if not isinstance(issue, Mapping):
                continue
            for field in (
                "description",
                "issue_description",
                "message",
                "suggested_fix",
                "recommendation",
            ):
                values.extend(_nested_text_values(issue.get(field)))
    return "\n".join(values)


def _nested_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        values: list[str] = []
        for item in value.values():
            values.extend(_nested_text_values(item))
        return values
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        values: list[str] = []
        for item in value:
            values.extend(_nested_text_values(item))
        return values
    return []


def _nested_mapping_keys(value: Any, selected: frozenset[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in selected:
                found.add(normalized_key)
            found.update(_nested_mapping_keys(item, selected))
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            found.update(_nested_mapping_keys(item, selected))
    return found


def _core_factor_text_errors(
    text: str,
    *,
    require_payment_success: bool = True,
    require_first_paid_users: bool = True,
) -> list[str]:
    errors: list[str] = []
    if not any(token in text for token in ("单笔付费金额", "平均订单金额")):
        errors.append("case_b_primary_core_driver_missing")
    if "付费频次" not in text:
        errors.append("case_b_frequency_factor_missing")
    if "付费人数" not in text:
        errors.append("case_b_paid_user_factor_missing")
    if require_first_paid_users and "首充" not in text:
        errors.append("case_b_first_paid_user_observation_missing")
    if require_payment_success and "支付成功率" not in text:
        errors.append("case_b_payment_success_boundary_missing")
    elif require_payment_success and not any(
        token in text
        for token in ("未观测", "缺少独立观测", "无独立观测", "中性", "按不变")
    ):
        errors.append("case_b_payment_success_neutral_assumption_missing")
    payment_success_overclaim = False
    for sentence in re.split(r"[。；;.!?？\n]+", text):
        if "支付成功率" not in sentence or not re.search(
            r"没有影响|无影响|零影响|已排除|观测为?100%",
            sentence,
        ):
            continue
        if any(
            marker in sentence
            for marker in (
                "不代表实际无影响",
                "不代表无影响",
                "不视为实际无影响",
                "不视为无影响",
                "不能说明无影响",
                "不能证明无影响",
                "并非无影响",
                "不得描述为无影响",
            )
        ):
            continue
        payment_success_overclaim = True
        break
    if payment_success_overclaim:
        errors.append("case_b_payment_success_overclaimed")
    first_paid_effect_overclaim = any(
        re.search(pattern, text)
        for pattern in (
            r"首充(?:人数|用户数|用户占比)?(?:自身)?(?:没有|无|未产生)(?:任何)?(?:影响|贡献)",
            r"首充(?:人数|用户数|用户占比)?.{0,8}对(?:总)?付费金额.{0,8}(?:没有|无|未产生|影响有限)",
            r"首充(?:人数|用户数|用户占比)?.{0,8}(?:影响|贡献)(?:很)?有限",
            r"首充(?:人数|用户数|用户占比)?.{0,12}是.{0,8}原因",
        )
    )
    if first_paid_effect_overclaim:
        errors.append("case_b_first_paid_user_effect_overclaimed")
    numeric_expectations = (
        ("case_b_amount_change_number_missing", ("1.35", "1.347", "409.77")),
        (
            "case_b_primary_contribution_number_missing",
            ("126", "517.24", "517万"),
        ),
        (
            "case_b_frequency_contribution_number_missing",
            ("-28", "−28", "-115", "−115", "116万"),
        ),
        (
            "case_b_paid_user_contribution_number_missing",
            ("2%", "2.0%", "8.11", "8万", "8.1万"),
        ),
    )
    for error, candidates in numeric_expectations:
        if not any(candidate in text for candidate in candidates):
            errors.append(error)
    if _global_core_block_wording(text):
        errors.append("case_b_auxiliary_gap_blocks_core")
    return errors


def _factor_state_narrative_errors(
    text: str,
    business_context: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    clauses = [
        clause.strip()
        for clause in re.split(r"[。；;.!?？\n]+", text)
        if clause.strip()
    ]
    for item in business_context.get("factorStates") or ():
        if not isinstance(item, Mapping):
            continue
        factor = str(item.get("factor") or "").strip()
        state = str(item.get("state") or "").strip()
        if not factor:
            continue
        factor_clauses = [
            clause for clause in clauses if factor in clause
        ]
        if state == "已观察变化，贡献尚未量化" and any(
            re.search(
                r"(?:可能|或许|也许|候选).{0,16}(?:影响|导致|带来|驱动|造成)",
                clause,
            )
            for clause in factor_clauses
        ):
            errors.append("case_b_observed_factor_mechanism_invented")
    return list(dict.fromkeys(errors))


def _global_core_block_wording(text: str) -> bool:
    return bool(
        re.search(
            r"(?:无法|不能|暂时无法).{0,18}(?:判断|分析|分解).{0,18}(?:主因|主要因素|核心因素|贡献)",
            text,
        )
    )


def _prequery_direction_asserted(text: str) -> bool:
    return bool(
        re.search(
            r"(?:已确认|已经确认|实际已|实际为|确实|数据表明)(?:为|是)?上涨",
            text,
        )
    )


def _output_prose(output: Mapping[str, Any]) -> str:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                collect(item)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for item in value:
                collect(item)

    collect(output)
    return "\n".join(values)


def _parse_response_object(content: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None, "response_not_json"
    if not isinstance(parsed, dict):
        return None, "response_not_object"
    return parsed, None


def _material_signature(
    job: BenchmarkJob,
    output: Mapping[str, Any] | None,
) -> str | None:
    if output is None:
        return None
    task = job.task
    fields = {
        "business_intent": (
            "question_family",
            "target_metric",
            "pattern_family",
            "pattern_params",
            "scope",
            "time_window",
            "baseline_candidates",
            "analysis_requirements",
        ),
        "boundary_decision": (
            "boundary_status",
            "recommended_assumption",
            "clarification_questions",
        ),
        "analysis_route_plan": ("requested_nodes", "analysis_requirements"),
        "final_route_narrative": ("sections",),
        "data_coverage_interpretation": ("coverage_status",),
        "next_action": ("next_action",),
        "causal_audit": ("causal_assessment",),
        "answer_synthesis": (),
        "answer_repair": (),
        "semantic_audit": ("audit_status", "issues"),
        "final_answer_audit": ("material_findings",),
    }.get(task)
    projection = (
        {field: output.get(field) for field in fields}
        if fields is not None
        else {}
    )
    projection["business_error_codes"] = sorted(
        _case_b_expectation_errors(job, output)
    )
    if task == "analysis_route_plan":
        projection["requested_nodes"] = sorted(
            str(item) for item in output.get("requested_nodes") or ()
        )
        requirements = output.get("analysis_requirements") or {}
        projection["analysis_requirements"] = _normalized_material_value(
            requirements,
            unordered_keys={
                "target_metrics",
                "requested_components",
                "requested_dimensions",
                "baselines",
                "context_sources",
                "dataset_requirements",
                "diagnostic_tags",
                "claim_intents",
                "goal_bindings",
                "component_ids",
                "dimension_ids",
                "context_source_ids",
            },
        )
    elif task == "boundary_decision":
        projection = {
            "boundary_status": output.get("boundary_status"),
            "question_count": len(output.get("clarification_questions") or ()),
            "recommends_previous_day": any(
                token in _output_prose(output)
                for token in ("前一天", "前一日")
            ),
            "business_error_codes": projection["business_error_codes"],
        }
    if task == "final_route_narrative":
        projection = {
            "step_refs": [
                section.get("step_ref")
                for section in output.get("sections") or ()
                if isinstance(section, Mapping)
            ],
            "business_error_codes": projection["business_error_codes"],
        }
    elif task == "semantic_audit":
        projection["issues"] = sorted(
            (
                {
                    "code": str(issue.get("code") or ""),
                    "severity": str(issue.get("severity") or ""),
                }
                for issue in output.get("issues") or ()
                if isinstance(issue, Mapping)
            ),
            key=lambda item: (item["severity"], item["code"]),
        )
    return _hash_json(projection)


def _normalized_material_value(
    value: Any,
    *,
    unordered_keys: set[str],
    parent_key: str = "",
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalized_material_value(
                item,
                unordered_keys=unordered_keys,
                parent_key=str(key),
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        normalized = [
            _normalized_material_value(
                item,
                unordered_keys=unordered_keys,
                parent_key=parent_key,
            )
            for item in value
        ]
        if parent_key in unordered_keys:
            return sorted(normalized, key=lambda item: _hash_json(item))
        return normalized
    return value


def _scenario_record(scenario: ReplayScenario) -> dict[str, Any]:
    record = asdict(scenario)
    record["required_keys"] = list(scenario.required_keys)
    record["messages"] = [dict(message) for message in scenario.messages]
    record["current_input_hash"] = _hash_json(scenario.messages)
    return record


def _scenario_listing(scenario: ReplayScenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "task": scenario.task,
        "provenance": scenario.provenance,
        "source_path": scenario.source_path,
        "source_task": scenario.source_task,
        "source_call_index": scenario.source_call_index,
        "source_input_hash": scenario.source_input_hash,
        "current_input_hash": _hash_json(scenario.messages),
        "prompt_version": scenario.prompt_version,
        "message_characters": sum(
            len(message.get("content") or "") for message in scenario.messages
        ),
    }


def _select_scenarios(
    scenarios: Sequence[ReplayScenario],
    selected_ids: Sequence[str],
    *,
    parser: argparse.ArgumentParser,
) -> list[ReplayScenario]:
    if not selected_ids:
        return list(scenarios)
    requested = set(selected_ids)
    available = {scenario.scenario_id for scenario in scenarios}
    unknown = sorted(requested - available)
    if unknown:
        parser.error("unknown --scenario: " + ", ".join(unknown))
    return [scenario for scenario in scenarios if scenario.scenario_id in requested]


def _validated_selection(
    values: Sequence[str] | None,
    *,
    allowed: Sequence[str],
    field: str,
) -> tuple[str, ...]:
    if not values:
        return tuple(allowed)
    selected = tuple(dict.fromkeys(str(value) for value in values))
    unknown = [value for value in selected if value not in allowed]
    if unknown:
        raise ValueError(f"unknown_{field}:" + ",".join(unknown))
    return selected


def _job_variants(jobs: Sequence[BenchmarkJob]) -> list[dict[str, str]]:
    variants: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for job in jobs:
        key = (job.model_tier, job.model, job.thinking)
        if key in seen:
            continue
        seen.add(key)
        variants.append(
            {
                "model_tier": job.model_tier,
                "model": job.model,
                "thinking": job.thinking,
            }
        )
    return variants


def _job_result_identity(job: BenchmarkJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "scenario_id": job.scenario_id,
        "task": job.task,
        "provenance": job.provenance,
        "model_tier": job.model_tier,
        "model": job.model,
        "thinking": job.thinking,
        "repeat": job.repeat,
        "prompt_version": job.prompt_version,
        "input_hash": job.input_hash,
    }


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        value = usage.model_dump()
    elif isinstance(usage, Mapping):
        value = dict(usage)
    else:
        return {}
    return _json_safe(value)


def _load_env_file(path: Path) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        os.environ[key] = value


def _narrative_values(output: Mapping[str, Any]) -> str:
    values: list[str] = []
    for field in ("route_summary", "decision_summary", "display_summary"):
        if isinstance(output.get(field), str):
            values.append(str(output[field]))
    for section in output.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        for field in ("route_step", "expected_evidence"):
            if isinstance(section.get(field), str):
                values.append(str(section[field]))
    return "\n".join(values)


def _safe_error_text(exc: Exception) -> str:
    text = str(exc).strip()
    text = re.sub(r"(?i)(api[_ -]?key)[=: ]+\S+", r"\1=[redacted]", text)
    return text[:2000]


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(float(values[0]), 3)
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(float(value), 3)


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(encoded)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_line(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)


def _write_json_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(_json_safe(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
