from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
import re
import signal
import threading
from time import perf_counter
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from openai import OpenAI


DEFAULT_TIMEOUT_SECONDS: float | None = None
DEFAULT_MAX_ATTEMPTS = 3
_RECEIVER_CLEANUP_JOIN_SECONDS = 1.0
_TASK_MATERIAL_OUTPUT_KEYS: dict[str, frozenset[str]] = {
    "business_intent": frozenset(
        {
            "question_family",
            "target_metric",
            "pattern_family",
            "scope",
            "time_window",
            "target_claim",
        }
    ),
}


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, Any]
    audit: dict[str, Any]


class LLMConfigurationError(RuntimeError):
    pass


class LLMOutputError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        audit: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.audit = dict(audit or {})


class LLMTimeoutError(RuntimeError):
    pass


class OpenAICompatibleLLMClient:
    supports_output_validator = True

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str = "",
        timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self._api_key = api_key
        self._request_worker: Callable[
            [dict[str, Any], Sequence[Mapping[str, str]]], dict[str, Any]
        ] | None = None
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout_seconds,
            max_retries=0,
        )

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "OpenAICompatibleLLMClient":
        env = os.environ if environ is None else environ
        provider = env.get("WAJE_LLM_PROVIDER", "openai").strip()
        model = env.get("WAJE_LLM_MODEL", "").strip()
        api_key = (
            env.get("WAJE_LLM_API_KEY")
            or env.get("OPENAI_API_KEY")
            or env.get("DEEPSEEK_API_KEY")
            or ""
        ).strip()
        base_url = env.get("WAJE_LLM_BASE_URL", "").strip()
        timeout_seconds = _parse_timeout_seconds(env.get("WAJE_LLM_TIMEOUT_SECONDS"))

        if provider not in {"openai", "openai_compatible"}:
            raise LLMConfigurationError("unsupported_llm_provider")
        if not model:
            raise LLMConfigurationError("missing_llm_model")
        if not api_key:
            raise LLMConfigurationError("missing_llm_api_key")
        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )

    def invoke_json(
        self,
        *,
        task: str,
        prompt_version: str,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        output_validator: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> LLMResult:
        started = perf_counter()
        started_at = _utc_now()
        messages_payload = [dict(message) for message in messages]
        attempt_failures: list[dict[str, Any]] = []
        response_payload: dict[str, Any] = {}
        content = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response_payload = {}
                response_payload = self._request_json_once(messages_payload, attempt=attempt)
                content = response_payload["content"] or "{}"
                output = _localize_narrative_fields(_parse_json_object(content))
                missing = [key for key in required_keys if key not in output]
                if missing:
                    raise LLMOutputError(f"missing_llm_output_keys:{','.join(missing)}")
                empty_material = [
                    key
                    for key in required_keys
                    if key in _TASK_MATERIAL_OUTPUT_KEYS.get(task, ())
                    and _empty_required_output_value(output[key])
                ]
                if empty_material:
                    raise LLMOutputError(
                        "empty_llm_output_keys:" + ",".join(empty_material)
                    )
                invalid_material = [
                    key
                    for key in required_keys
                    if key in _TASK_MATERIAL_OUTPUT_KEYS.get(task, ())
                    and not _valid_task_material_output(key, output[key])
                ]
                if invalid_material:
                    raise LLMOutputError(
                        "invalid_llm_output_material:" + ",".join(invalid_material)
                    )
                if output_validator is not None:
                    output_validator(output)
                break
            except Exception as exc:
                failure_code = _safe_retry_failure_code(exc)
                attempt_failures.append(
                    {
                        "attempt": attempt,
                        "failure_code": failure_code,
                        "response_id": str(response_payload.get("response_id") or ""),
                    }
                )
                if attempt >= self.max_attempts:
                    audit = _failed_llm_audit(
                        task=task,
                        provider=self.provider,
                        model=self.model,
                        prompt_version=prompt_version,
                        required_keys=required_keys,
                        messages=messages_payload,
                        base_url=self.base_url,
                        started_at=started_at,
                        started=started,
                        attempt=attempt,
                        response_payload=response_payload,
                        failure_code=failure_code,
                        attempt_failures=attempt_failures,
                    )
                    if isinstance(exc, LLMOutputError):
                        raise LLMOutputError(str(exc), audit=audit) from exc
                    try:
                        setattr(exc, "audit", audit)
                    except Exception:
                        pass
                    raise
        finished_at = _utc_now()

        return LLMResult(
            output=output,
            audit={
                "task": task,
                "provider": self.provider,
                "model": self.model,
                "prompt_version": prompt_version,
                "response_id": response_payload.get("response_id", ""),
                "messages": messages_payload,
                "required_keys": list(required_keys),
                "raw_response_content": content,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "attempt_count": attempt,
                "input_hash": _hash_json(messages),
                "output_hash": _hash_json(output),
                "base_url_hash": _hash_text(self.base_url) if self.base_url else "",
                "usage": dict(response_payload.get("usage") or {}),
                "structured_output": output,
            },
        )

    def _request_json_once(
        self, messages: Sequence[Mapping[str, str]], *, attempt: int = 1
    ) -> dict[str, Any]:
        if isinstance(self._client, OpenAI):
            return _request_openai_json_in_subprocess(
                {
                    "api_key": self._api_key,
                    "base_url": self.base_url,
                    "timeout_seconds": self.timeout_seconds,
                    "model": self.model,
                    "attempt": attempt,
                },
                [dict(message) for message in messages],
                self.timeout_seconds,
                request_worker=self._request_worker or _request_openai_json_once,
            )
        with _wall_clock_timeout(self.timeout_seconds):
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[dict(message) for message in messages],
                response_format={"type": "json_object"},
                temperature=0,
            )
        return {
            "response_id": getattr(response, "id", ""),
            "content": response.choices[0].message.content or "{}",
            "usage": _usage_dict(getattr(response, "usage", None)),
        }


def _empty_required_output_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping):
        return not value
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return not value
    return False


def _valid_task_material_output(key: str, value: Any) -> bool:
    if key != "time_window":
        return isinstance(value, str) and bool(value.strip())
    return _valid_json_business_semantics(value)


def _valid_json_business_semantics(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, Mapping):
        return bool(value) and all(
            isinstance(key, str)
            and bool(key.strip())
            and _valid_json_business_semantics(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return bool(value) and all(_valid_json_business_semantics(item) for item in value)
    return False


def _safe_retry_failure_code(exc: Exception) -> str:
    if isinstance(exc, (LLMOutputError, LLMTimeoutError, LLMConfigurationError)):
        return str(exc).strip() or type(exc).__name__
    return type(exc).__name__


def _failed_llm_audit(
    *,
    task: str,
    provider: str,
    model: str,
    prompt_version: str,
    required_keys: Sequence[str],
    messages: Sequence[Mapping[str, str]],
    base_url: str,
    started_at: str,
    started: float,
    attempt: int,
    response_payload: Mapping[str, Any],
    failure_code: str,
    attempt_failures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "task": task,
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "response_id": str(response_payload.get("response_id") or ""),
        "required_keys": list(required_keys),
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_ms": round((perf_counter() - started) * 1000, 3),
        "attempt_count": attempt,
        "input_hash": _hash_json(messages),
        "base_url_hash": _hash_text(base_url) if base_url else "",
        "usage": dict(response_payload.get("usage") or {}),
        "status": "failed",
        "failure_code": failure_code,
        "attempt_failures": [dict(item) for item in attempt_failures],
    }


def _request_openai_json_in_subprocess(
    config: dict[str, Any],
    messages: Sequence[Mapping[str, str]],
    timeout_seconds: float | None,
    *,
    request_worker: Callable[
        [dict[str, Any], Sequence[Mapping[str, str]]], dict[str, Any]
    ] | None = None,
) -> dict[str, Any]:
    request_worker = request_worker or _request_openai_json_once
    ctx = _process_context()
    output_connection, child_connection = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_openai_request_child,
        args=(
            config,
            [dict(message) for message in messages],
            child_connection,
            request_worker,
        ),
    )
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None and float(timeout_seconds) > 0
        else None
    )
    deadline = perf_counter() + timeout if timeout is not None else None
    started = False
    timeout_expired = False
    receive_state: dict[str, Any] = {}
    receiver: threading.Thread | None = None

    def receive_result() -> None:
        try:
            receive_state["result"] = output_connection.recv()
        except BaseException as exc:
            receive_state["error"] = exc

    try:
        process.start()
        started = True
        child_connection.close()
        receiver = threading.Thread(
            target=receive_result,
            name="waje-llm-provider-receiver",
            daemon=True,
        )
        receiver.start()
        receive_wait = (
            None
            if deadline is None
            else max(0.0, deadline - perf_counter())
        )
        receiver.join(receive_wait)
        if receiver.is_alive():
            timeout_expired = True
            raise LLMTimeoutError(f"llm_request_timeout:{timeout:g}s")
        if deadline is not None and perf_counter() >= deadline:
            timeout_expired = True
            raise LLMTimeoutError(f"llm_request_timeout:{timeout:g}s")

        remaining = (
            None
            if deadline is None
            else max(0.0, deadline - perf_counter())
        )
        process.join(remaining)
        if process.is_alive():
            timeout_expired = True
            raise LLMTimeoutError(f"llm_request_timeout:{timeout:g}s")
        if process.exitcode != 0:
            raise RuntimeError(
                f"llm_subprocess_failed:exitcode={process.exitcode}"
            )
        receive_error = receive_state.get("error")
        if receive_error is not None:
            raise RuntimeError(
                f"llm_subprocess_failed:exitcode={process.exitcode}"
            ) from receive_error
        child_result = receive_state.get("result")
    finally:
        receiver_cleanup_failed = False
        child_connection.close()
        if started:
            if process.is_alive():
                if timeout_expired and timeout is not None:
                    process.kill()
                process.join()
            else:
                process.join()
        output_connection.close()
        if receiver is not None:
            receiver.join(_RECEIVER_CLEANUP_JOIN_SECONDS)
            if receiver.is_alive():
                receiver_cleanup_failed = True
        if started:
            process.close()
        if receiver_cleanup_failed:
            raise RuntimeError("llm_receiver_cleanup_timeout")
    if not isinstance(child_result, Mapping):
        raise RuntimeError("llm_subprocess_invalid_result")
    if not child_result.get("ok"):
        raise RuntimeError(str(child_result.get("error") or "llm_subprocess_error"))
    result = child_result.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("llm_subprocess_invalid_result")
    return dict(result)


def _process_context():
    try:
        ctx = multiprocessing.get_context("spawn")
    except ValueError as exc:
        raise LLMConfigurationError("spawn_start_method_unavailable") from exc
    if ctx.get_start_method() != "spawn":
        raise LLMConfigurationError("spawn_start_method_unavailable")
    return ctx


def _openai_request_child(
    config: dict[str, Any],
    messages: Sequence[Mapping[str, str]],
    output_connection: Any,
    request_worker: Callable[[dict[str, Any], Sequence[Mapping[str, str]]], dict[str, Any]],
) -> None:
    try:
        output_connection.send(
            {
                "ok": True,
                "result": request_worker(config, messages),
            }
        )
    except BaseException as exc:
        try:
            output_connection.send({"ok": False, "error": str(exc)})
        except BaseException:
            pass
    finally:
        output_connection.close()


def _request_openai_json_once(
    config: dict[str, Any],
    messages: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    client = OpenAI(
        api_key=config["api_key"],
        base_url=config.get("base_url") or None,
        timeout=config["timeout_seconds"],
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=config["model"],
        messages=[dict(message) for message in messages],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return {
        "response_id": getattr(response, "id", ""),
        "content": response.choices[0].message.content or "{}",
        "usage": _usage_dict(getattr(response, "usage", None)),
    }


@contextmanager
def _wall_clock_timeout(seconds: float | None) -> Iterator[None]:
    if seconds is None or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise_timeout(signum, frame):
        raise LLMTimeoutError(f"llm_request_timeout:{seconds:g}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _parse_timeout_seconds(timeout_text: str | None) -> float | None:
    if timeout_text is None:
        return None
    normalized = timeout_text.strip().lower()
    if normalized in {"", "0", "none", "disabled", "off", "false", "no"}:
        return None
    try:
        timeout_seconds = float(normalized)
    except ValueError as exc:
        raise LLMConfigurationError("invalid_llm_timeout") from exc
    if timeout_seconds <= 0:
        raise LLMConfigurationError("invalid_llm_timeout")
    return timeout_seconds


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise LLMOutputError("llm_output_not_json")
        loaded = json.loads(match.group(0))
    if not isinstance(loaded, dict):
        raise LLMOutputError("llm_output_not_object")
    return loaded


NARRATIVE_KEYS = frozenset(
    {
        "status_message",
        "decision_summary",
        "recommended_assumption",
        "route_summary",
        "repair_summary",
        "business_impact",
        "interpretation",
        "evidence_boundary",
        "answer_text",
        "summary_text",
        "text",
        "explanation",
        "repair_path",
        "owner",
        "recommendation_reason",
        "question",
        "accepted_assumptions",
        "business_summary",
        "description",
        "issue_description",
    }
)

def _localize_narrative_fields(value: Any, key: str = "") -> Any:
    if key == "accepted_assumptions" and isinstance(value, str):
        raise LLMOutputError("llm_narrative_invalid:accepted_assumptions")
    if key == "accepted_assumptions" and isinstance(value, list):
        normalized_items = []
        for item in value:
            if isinstance(item, str):
                normalized = _normalize_business_narrative(item.strip())
                if not normalized or _contains_unlocalized_narrative_tokens(normalized):
                    raise LLMOutputError(
                        "llm_narrative_invalid:accepted_assumptions"
                    )
                normalized_items.append(normalized)
            else:
                raise LLMOutputError(
                    "llm_narrative_invalid:accepted_assumptions"
                )
        return normalized_items
    if key == "recommended_assumption" and isinstance(value, Mapping):
        return {
            item_key: _localize_narrative_fields(item, item_key)
            for item_key, item in value.items()
        }
    if (
        key in NARRATIVE_KEYS
        and key != "accepted_assumptions"
        and not isinstance(value, str)
    ):
        raise LLMOutputError(f"llm_narrative_invalid:{key}")
    if isinstance(value, dict):
        return {item_key: _localize_narrative_fields(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_localize_narrative_fields(item, key) for item in value]
    if isinstance(value, str) and key in NARRATIVE_KEYS:
        value = _normalize_business_narrative(value)
        if not value.strip() or _contains_unlocalized_narrative_tokens(value):
            raise LLMOutputError(f"llm_narrative_invalid:{key}")
    return value


def _normalize_business_narrative(value: str) -> str:
    value = re.sub(
        r"\bdraft_claims\s+and\s+evidence_brief\s+disagree\s+with\s+wording_limit\s+for\s+paid_amount\.?",
        "答案声明、证据摘要和措辞边界对于付费金额的表述不一致。",
        value,
    )
    value = re.sub(r"\(min_periods=(\d+)\)", r"（至少\1个可比周期）", value)
    value = re.sub(r"min_periods=(\d+)", r"至少\1个可比周期", value)
    value = re.sub(r"pattern_status\s*:\s*high", "模式证据强度高", value)
    value = re.sub(r"pattern_status\s*:\s*medium", "模式证据强度中等", value)
    value = re.sub(r"pattern_status\s*:\s*low", "模式证据强度低", value)
    value = re.sub(r"pattern_status\s*:\s*insufficient", "模式证据不足", value)
    value = re.sub(r"allow_question_interrupt\s*=\s*false", "当前不打断用户提问", value)
    value = re.sub(
        r"至少\s*\d+(?:\.\d+)?%\s*的(月份|周期|周|季度|日期|窗口)中",
        r"足够多的\1中",
        value,
    )
    value = re.sub(r"\bmedium\b", "中等", value)
    value = re.sub(r"\bfalse\b", "否", value)
    value = re.sub(r"\btrue\b", "是", value)
    value = re.sub(r"(?<![A-Za-z])vs(?![A-Za-z])", "相比", value, flags=re.IGNORECASE)
    value = re.sub(r"[（(]\s*例如\s*[^）)]*[%％][^）)]*[）)]", "", value)
    value = re.sub(
        r"[（(]\s*product default materiality and stability rules\s*[）)]",
        "",
        value,
        flags=re.IGNORECASE,
    )
    replacements = {
        "product default materiality and stability rules": "产品默认的重要性和稳定性规则",
        "材料阈值": "重要性阈值",
        "材料性": "重要性",
        "物质性下限": "重要性下限",
        "物质性": "重要性",
        "显著阈值": "重要性阈值",
        "稳定可靠": "本次对比结论成立",
        "显著性水平": "重要性规则",
        "显著性": "重要性",
        "统计显著": "符合重要性规则",
        "显著": "明显",
        "p-value": "重要性规则",
        "p值": "重要性规则",
        "置信水平": "重要性规则",
        "置信度": "结论强度",
        "Pattern": "模式",
        "pattern_status": "模式证据状态",
        "pattern_established": "模式是否成立",
        "wording_limit": "措辞边界",
        "draft_claims": "答案声明",
        "evidence_brief": "证据摘要",
        "allow_question_interrupt": "是否允许打断提问",
        "synthesize_answer": "进入答案合成",
        "degrade": "降级处理",
        "paid_amount": "付费金额",
        "metric_coverage_profile": "指标覆盖检查",
        "metric_timeseries": "指标时间序列",
        "gameplay_activity_context": "玩法活动上下文",
        "data_quality_profile": "数据质量检查",
        "compare_periods": "周期对比",
        "compare_period_phases": "周期内阶段对比",
        "rolling_window_compare": "滚动窗口对比",
        "weekday_calendar_compare": "星期日历对比",
        "event_window_compare": "事件窗口对比",
        "formula_decompose": "公式拆解",
        "driver_decomposition": "驱动拆解",
        "segment_contribution": "渠道或分群贡献",
        "segment_breakdown": "分群拆解",
        "candidate_dimension_screen": "候选维度筛选",
        "joint_attribution": "组合归因",
        "outlier_scan": "异常周期检查",
        "outlier_contribution": "异常贡献检查",
        "change_point_scan": "变化点检查",
        "evidence_reduce": "证据整理",
        "answer_verify": "答案校验",
        "paid_amount_change_explanation": "付费金额变化解释",
        "pattern_explanation": "模式解释",
        "business_object_impact_review": "业务对象影响评估",
        "revenue_health_review": "收入健康评估",
        "segment_or_factor_attribution": "分群或因素归因",
        "anomaly_or_black_swan_review": "异常或突发因素评估",
        "custom_baseline_comparison": "自定义基线对比",
        "data_quality_or_evidence_review": "数据质量或证据评估",
        "research": "研发",
        "scope": "范围",
        "full_sample": "全样本",
        "mid_phase": "月中窗口",
        "pattern_params": "窗口规则",
        "pattern_family": "模式类型",
        "模式参数": "窗口规则",
        "target_claim": "目标结论",
        "baseline_candidates": "基线候选",
        "boundary_status": "边界状态",
        "phase4_policy": "当前策略",
        "claim strength": "结论强度",
        "对账单强度": "结论强度",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    value = value.replace("模式确认性高", "本次对比证据较强")
    value = value.replace("模式本次对比结论成立", "本次对比结论成立")
    value = value.replace("数据质量检查检查", "数据质量检查能力检查")
    return value


_BUSINESS_ENTITY_SUFFIX_PATTERN = (
    r"渠道|产品|版本|活动|页面|地区|国家|市场|用户|客群|分群|人群|订单|"
    r"方案|团队|公司|门店|城市|币种|广告|素材|来源|端|业务线|套餐|计划|等级|标签"
)

_REVIEWED_BUSINESS_NARRATIVE_ACRONYMS = frozenset(
    {"ARPPU", "DAU", "NGN", "ROI", "WAJE"}
)


def _strip_business_entity_tokens(value: str) -> str:
    return re.sub(
        rf"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?=({_BUSINESS_ENTITY_SUFFIX_PATTERN}))",
        "",
        value,
    )


def _contains_unlocalized_narrative_tokens(value: str) -> bool:
    if not re.search(r"[\u3400-\u9fff]", value):
        return True
    value = _strip_business_entity_tokens(value)
    for acronym in _REVIEWED_BUSINESS_NARRATIVE_ACRONYMS:
        value = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(acronym)}(?![A-Za-z0-9])",
            "",
            value,
        )
    return bool(re.search(r"[A-Za-z]{2,}", value))


def _hash_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _hash_text(text)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    data = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    return data
