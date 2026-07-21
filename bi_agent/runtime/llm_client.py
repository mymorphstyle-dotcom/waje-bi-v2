from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
import signal
import threading
from time import perf_counter
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence
from urllib.parse import urlparse

from openai import (
    APIConnectionError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)


DEFAULT_TIMEOUT_SECONDS: float | None = None
DEFAULT_MAX_ATTEMPTS = 3
_SUBPROCESS_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, Any]
    audit: dict[str, Any]


class LLMConfigurationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.audit = dict(audit or {})


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
    def __init__(
        self,
        message: str,
        *,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.audit = dict(audit or {})


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        *,
        kind: str,
        retryability: str,
        status_code: int | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
        error_param: str | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        if kind not in {
            "provider_authentication_failed",
            "provider_permission_denied",
            "provider_rate_limited",
            "provider_request_rejected",
            "provider_timeout",
            "provider_unavailable",
        }:
            raise ValueError("llm_provider_failure_kind_invalid")
        if retryability not in {"retryable", "not_retryable"}:
            raise ValueError("llm_provider_failure_retryability_invalid")
        if status_code is not None and (
            isinstance(status_code, bool) or not isinstance(status_code, int)
        ):
            raise ValueError("llm_provider_failure_status_code_invalid")
        for value in (error_code, error_type, error_param):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("llm_provider_failure_detail_invalid")
        super().__init__(kind)
        self.kind = kind
        self.retryability = retryability
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type
        self.error_param = error_param
        self.provider_error = {
            key: value
            for key, value in (
                ("status_code", status_code),
                ("code", error_code),
                ("type", error_type),
                ("param", error_param),
            )
            if value is not None
        }
        self.audit = dict(audit or {})


class OpenAICompatibleLLMClient:
    supports_output_validator = True
    supports_model_tier = True
    supports_thinking_mode = True

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        critical_model: str = "",
        api_key: str,
        base_url: str = "",
        timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        self.provider = provider
        self.model = model
        self.critical_model = critical_model or model
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise LLMConfigurationError("invalid_llm_max_attempts")
        self.durable_max_attempts = max_attempts
        self._api_key = api_key
        self._request_worker: (
            Callable[[dict[str, Any], Sequence[Mapping[str, str]]], dict[str, Any]]
            | None
        ) = None
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
        critical_model = env.get("WAJE_LLM_CRITICAL_MODEL", "").strip()
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
            critical_model=critical_model,
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
        model_tier: str = "default",
        thinking: str | None = None,
    ) -> LLMResult:
        if model_tier not in {"default", "critical"}:
            raise LLMConfigurationError("invalid_llm_model_tier")
        if thinking not in {None, "enabled", "disabled"}:
            raise LLMConfigurationError("invalid_llm_thinking_mode")
        started = perf_counter()
        started_at = _utc_now()
        messages_payload = [dict(message) for message in messages]
        response_payload: dict[str, Any] = {}
        content = ""
        actual_model = self.critical_model if model_tier == "critical" else self.model
        parsed_output: dict[str, Any] | None = None
        try:
            response_payload = self._request_json_once(
                messages_payload,
                attempt=1,
                model=actual_model,
                thinking=thinking,
            )
            content = response_payload["content"]
            parsed_output = _parse_json_object(content)
            output = parsed_output
            missing = [key for key in required_keys if key not in output]
            if missing:
                raise LLMOutputError(f"missing_llm_output_keys:{','.join(missing)}")
            if output_validator is not None:
                try:
                    output_validator(output)
                except ValueError as exc:
                    raise LLMOutputError(
                        str(exc).strip() or "llm_output_contract_invalid"
                    ) from exc
        except Exception as exc:
            failure_code = llm_failure_code(exc)
            attempt_failure = {
                "attempt": 1,
                "failure_code": failure_code,
                "response_id": str(response_payload.get("response_id") or ""),
                "reasoning_content_present": bool(
                    response_payload.get("reasoning_content_present", False)
                ),
            }
            if content:
                attempt_failure["raw_response_digest"] = _hash_text(content)
                attempt_failure["raw_response_bytes"] = len(content.encode("utf-8"))
            if parsed_output is not None:
                attempt_failure["structured_output_digest"] = _hash_json(parsed_output)
                attempt_failure["structured_output_bytes"] = _json_byte_count(
                    parsed_output
                )
            if isinstance(exc, LLMProviderError) and exc.provider_error:
                attempt_failure["provider_error"] = dict(exc.provider_error)
            audit = _failed_llm_audit(
                task=task,
                provider=self.provider,
                model=actual_model,
                model_tier=model_tier,
                thinking=thinking,
                prompt_version=prompt_version,
                required_keys=required_keys,
                messages=messages_payload,
                base_url=self.base_url,
                started_at=started_at,
                started=started,
                attempt=1,
                response_payload=response_payload,
                failure_code=failure_code,
                attempt_failures=(attempt_failure,),
            )
            if isinstance(exc, LLMOutputError):
                raise LLMOutputError(str(exc), audit=audit) from exc
            if isinstance(exc, LLMProviderError):
                raise LLMProviderError(
                    kind=exc.kind,
                    retryability=exc.retryability,
                    status_code=exc.status_code,
                    error_code=exc.error_code,
                    error_type=exc.error_type,
                    error_param=exc.error_param,
                    audit=audit,
                ) from exc
            if isinstance(exc, LLMTimeoutError):
                raise LLMTimeoutError(str(exc), audit=audit) from exc
            if isinstance(exc, LLMConfigurationError):
                raise LLMConfigurationError(str(exc), audit=audit) from exc
            raise
        finished_at = _utc_now()

        audit = {
            "task": task,
            "provider": self.provider,
            "model": actual_model,
            "model_tier": model_tier,
            "thinking": thinking,
            "reasoning_content_present": bool(
                response_payload.get("reasoning_content_present", False)
            ),
            "prompt_version": prompt_version,
            "response_id": response_payload.get("response_id", ""),
            "messages": messages_payload,
            "required_keys": list(required_keys),
            "raw_response_content": content,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "attempt_count": 1,
            "input_hash": _hash_json(messages),
            "output_hash": _hash_json(output),
            "base_url_hash": _hash_text(self.base_url) if self.base_url else "",
            "usage": dict(response_payload.get("usage") or {}),
            "structured_output": output,
        }
        return LLMResult(output=output, audit=audit)

    def _request_json_once(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        attempt: int = 1,
        model: str | None = None,
        thinking: str | None = None,
    ) -> dict[str, Any]:
        actual_model = model or self.model
        if isinstance(self._client, OpenAI):
            return _request_openai_json_in_subprocess(
                {
                    "api_key": self._api_key,
                    "base_url": self.base_url,
                    "timeout_seconds": self.timeout_seconds,
                    "model": actual_model,
                    "thinking": thinking,
                    "deepseek_endpoint": _is_deepseek_endpoint(self.base_url),
                    "attempt": attempt,
                },
                [dict(message) for message in messages],
                self.timeout_seconds,
                request_worker=self._request_worker or _request_openai_json_once,
            )
        try:
            with _wall_clock_timeout(self.timeout_seconds):
                request = _chat_completion_request(
                    model=actual_model,
                    messages=messages,
                    thinking=thinking,
                    deepseek_endpoint=_is_deepseek_endpoint(self.base_url),
                )
                response = self._client.chat.completions.create(**request)
        except OpenAIError as exc:
            raise _llm_provider_error_from_openai(exc) from exc
        message = response.choices[0].message
        return {
            "response_id": getattr(response, "id", ""),
            "content": message.content or "",
            "usage": _usage_dict(getattr(response, "usage", None)),
            "reasoning_content_present": bool(
                getattr(message, "reasoning_content", None)
            ),
        }


def llm_failure_code(exc: Exception) -> str:
    if isinstance(
        exc,
        (LLMOutputError, LLMTimeoutError, LLMConfigurationError, LLMProviderError),
    ):
        return str(exc).strip() or type(exc).__name__
    return type(exc).__name__


def llm_failure_is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (LLMOutputError, LLMTimeoutError)):
        return True
    return isinstance(exc, LLMProviderError) and exc.retryability == "retryable"


def _failed_llm_audit(
    *,
    task: str,
    provider: str,
    model: str,
    model_tier: str,
    thinking: str | None,
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
        "model_tier": model_tier,
        "thinking": thinking,
        "reasoning_content_present": bool(
            response_payload.get("reasoning_content_present", False)
        ),
        "prompt_version": prompt_version,
        "response_id": str(response_payload.get("response_id") or ""),
        "required_keys": list(required_keys),
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_ms": round((perf_counter() - started) * 1000, 3),
        "attempt_count": attempt,
        "input_hash": _hash_json(messages),
        "input_bytes": _json_byte_count(messages),
        "input_message_count": len(messages),
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
    ]
    | None = None,
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
    child_result: Any = None

    try:
        process.start()
        started = True
        child_connection.close()
        while True:
            remaining = (
                None if deadline is None else max(0.0, deadline - perf_counter())
            )
            receive_wait = (
                _SUBPROCESS_POLL_SECONDS
                if remaining is None
                else min(_SUBPROCESS_POLL_SECONDS, remaining)
            )
            if output_connection.poll(receive_wait):
                try:
                    child_result = output_connection.recv()
                except (EOFError, OSError) as exc:
                    process.join()
                    raise RuntimeError(
                        f"llm_subprocess_failed:exitcode={process.exitcode}"
                    ) from exc
                break
            if not process.is_alive():
                process.join()
                raise RuntimeError(
                    f"llm_subprocess_failed:exitcode={process.exitcode}"
                )
            if deadline is not None and perf_counter() >= deadline:
                timeout_expired = True
                raise LLMTimeoutError(f"llm_request_timeout:{timeout:g}s")

        remaining = None if deadline is None else max(0.0, deadline - perf_counter())
        process.join(remaining)
        if process.is_alive():
            timeout_expired = True
            raise LLMTimeoutError(f"llm_request_timeout:{timeout:g}s")
        if process.exitcode != 0:
            raise RuntimeError(f"llm_subprocess_failed:exitcode={process.exitcode}")
    finally:
        child_connection.close()
        if started:
            if process.is_alive():
                if timeout_expired and timeout is not None:
                    process.kill()
                process.join()
            else:
                process.join()
        output_connection.close()
        if started:
            process.close()
    if not isinstance(child_result, Mapping):
        raise RuntimeError("llm_subprocess_invalid_result")
    if not child_result.get("ok"):
        failure_kind = child_result.get("failure_kind")
        if failure_kind == "provider_error" and set(child_result) == {
            "ok",
            "failure_kind",
            "kind",
            "retryability",
            "provider_error",
        }:
            provider_error = child_result["provider_error"]
            if not isinstance(provider_error, Mapping) or not set(
                provider_error
            ).issubset({"status_code", "code", "type", "param"}):
                raise RuntimeError("llm_subprocess_invalid_result")
            try:
                error = LLMProviderError(
                    kind=str(child_result["kind"]),
                    retryability=str(child_result["retryability"]),
                    status_code=provider_error.get("status_code"),
                    error_code=provider_error.get("code"),
                    error_type=provider_error.get("type"),
                    error_param=provider_error.get("param"),
                )
            except ValueError as exc:
                raise RuntimeError("llm_subprocess_invalid_result") from exc
            raise error
        if failure_kind == "worker_error" and set(child_result) == {
            "ok",
            "failure_kind",
            "error_type",
        }:
            error_type = child_result.get("error_type")
            if not isinstance(error_type, str) or not error_type:
                raise RuntimeError("llm_subprocess_invalid_result")
            raise RuntimeError(f"llm_subprocess_worker_failed:{error_type}")
        raise RuntimeError("llm_subprocess_invalid_result")
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
    request_worker: Callable[
        [dict[str, Any], Sequence[Mapping[str, str]]], dict[str, Any]
    ],
) -> None:
    try:
        output_connection.send(
            {
                "ok": True,
                "result": request_worker(config, messages),
            }
        )
    except LLMProviderError as exc:
        try:
            output_connection.send(
                {
                    "ok": False,
                    "failure_kind": "provider_error",
                    "kind": exc.kind,
                    "retryability": exc.retryability,
                    "provider_error": dict(exc.provider_error),
                }
            )
        except BaseException:
            pass
    except OpenAIError as exc:
        provider_error = _llm_provider_error_from_openai(exc)
        try:
            output_connection.send(
                {
                    "ok": False,
                    "failure_kind": "provider_error",
                    "kind": provider_error.kind,
                    "retryability": provider_error.retryability,
                    "provider_error": dict(provider_error.provider_error),
                }
            )
        except BaseException:
            pass
    except BaseException as exc:
        try:
            output_connection.send(
                {
                    "ok": False,
                    "failure_kind": "worker_error",
                    "error_type": type(exc).__name__,
                }
            )
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
        **_chat_completion_request(
            model=str(config["model"]),
            messages=messages,
            thinking=(
                str(config["thinking"])
                if config.get("thinking") in {"enabled", "disabled"}
                else None
            ),
            deepseek_endpoint=bool(
                config.get(
                    "deepseek_endpoint",
                    _is_deepseek_endpoint(str(config.get("base_url") or "")),
                )
            ),
        )
    )
    message = response.choices[0].message
    return {
        "response_id": getattr(response, "id", ""),
        "content": message.content or "",
        "usage": _usage_dict(getattr(response, "usage", None)),
        "reasoning_content_present": bool(getattr(message, "reasoning_content", None)),
    }


def _llm_provider_error_from_openai(exc: OpenAIError) -> LLMProviderError:
    diagnostics = _openai_provider_error_diagnostics(exc)
    if isinstance(exc, APITimeoutError):
        return LLMProviderError(
            kind="provider_timeout",
            retryability="retryable",
            **diagnostics,
        )
    if isinstance(exc, RateLimitError):
        return LLMProviderError(
            kind="provider_rate_limited",
            retryability="retryable",
            **diagnostics,
        )
    if isinstance(exc, APIConnectionError):
        return LLMProviderError(
            kind="provider_unavailable",
            retryability="retryable",
            **diagnostics,
        )
    status_code = getattr(exc, "status_code", None)
    if status_code == 401:
        return LLMProviderError(
            kind="provider_authentication_failed",
            retryability="not_retryable",
            **diagnostics,
        )
    if status_code == 403:
        return LLMProviderError(
            kind="provider_permission_denied",
            retryability="not_retryable",
            **diagnostics,
        )
    if status_code in {408, 409, 429} or (
        isinstance(status_code, int) and status_code >= 500
    ):
        return LLMProviderError(
            kind="provider_unavailable",
            retryability="retryable",
            **diagnostics,
        )
    return LLMProviderError(
        kind="provider_request_rejected",
        retryability="not_retryable",
        **diagnostics,
    )


def _openai_provider_error_diagnostics(exc: OpenAIError) -> dict[str, Any]:
    body = getattr(exc, "body", None)
    structured_error: Mapping[str, Any] = {}
    if isinstance(body, Mapping):
        nested_error = body.get("error")
        structured_error = nested_error if isinstance(nested_error, Mapping) else body

    diagnostics: dict[str, Any] = {}
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        diagnostics["status_code"] = status_code
    for source_name, target_name in (
        ("code", "error_code"),
        ("type", "error_type"),
        ("param", "error_param"),
    ):
        value = getattr(exc, source_name, None)
        if value is None:
            value = structured_error.get(source_name)
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str) and value:
            diagnostics[target_name] = value
    return diagnostics


def _chat_completion_request(
    *,
    model: str,
    messages: Sequence[Mapping[str, str]],
    thinking: str | None,
    deepseek_endpoint: bool,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": [dict(message) for message in messages],
        "response_format": {"type": "json_object"},
    }
    if deepseek_endpoint and thinking in {"enabled", "disabled"}:
        request["extra_body"] = {"thinking": {"type": thinking}}
    if thinking != "enabled":
        request["temperature"] = 0
    return request


def _is_deepseek_endpoint(base_url: str) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return hostname == "deepseek.com" or hostname.endswith(".deepseek.com")


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
    except json.JSONDecodeError as exc:
        raise LLMOutputError("llm_output_not_json") from exc
    if not isinstance(loaded, dict):
        raise LLMOutputError("llm_output_not_object")
    return loaded


def parse_llm_structured_response_content(content: str) -> dict[str, Any]:
    return _parse_json_object(content)


def _hash_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _hash_text(text)


def _json_byte_count(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return len(text.encode("utf-8"))


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
