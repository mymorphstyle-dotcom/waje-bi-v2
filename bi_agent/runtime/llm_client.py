from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any, Mapping, Optional, Sequence

from openai import OpenAI


DEFAULT_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, Any]
    audit: dict[str, Any]


class LLMConfigurationError(RuntimeError):
    pass


class LLMOutputError(RuntimeError):
    pass


class OpenAICompatibleLLMClient:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout_seconds,
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
        timeout_text = env.get("WAJE_LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout_seconds = int(timeout_text)
        except ValueError as exc:
            raise LLMConfigurationError("invalid_llm_timeout") from exc

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
    ) -> LLMResult:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[dict(message) for message in messages],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        output = _parse_json_object(content)
        missing = [key for key in required_keys if key not in output]
        if missing:
            raise LLMOutputError(f"missing_llm_output_keys:{','.join(missing)}")

        return LLMResult(
            output=output,
            audit={
                "task": task,
                "provider": self.provider,
                "model": self.model,
                "prompt_version": prompt_version,
                "response_id": getattr(response, "id", ""),
                "input_hash": _hash_json(messages),
                "output_hash": _hash_json(output),
                "base_url_hash": _hash_text(self.base_url) if self.base_url else "",
                "usage": _usage_dict(getattr(response, "usage", None)),
                "structured_output": output,
            },
        )


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


def _hash_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _hash_text(text)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    data = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    return data
