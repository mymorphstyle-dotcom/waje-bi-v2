from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Mapping

from agents.tracing import get_trace_provider, set_trace_processors
from agents.tracing.processor_interface import TracingProcessor

from bi_agent.runtime.agent_sdk_contracts import AgentTraceSink


@dataclass(frozen=True)
class AgentTraceStoragePolicy:
    max_record_bytes: int = 524_288
    max_records_per_run: int = 256
    retention_days: int = 30

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_record_bytes, bool)
            or self.max_record_bytes < 1
            or isinstance(self.max_records_per_run, bool)
            or self.max_records_per_run < 1
            or isinstance(self.retention_days, bool)
            or self.retention_days < 1
        ):
            raise ValueError("agent_trace_storage_policy_invalid")


DEFAULT_AGENT_TRACE_STORAGE_POLICY = AgentTraceStoragePolicy()


class AgentTraceStorageError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def waje_sdk_trace_id(run_id: str) -> str:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
    return f"trace_{digest}"


class InMemoryAgentTraceSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(deepcopy(self._records))

    def write_trace_record(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._records.append(deepcopy(dict(record)))


class PostgresAgentTraceSink:
    """Persists full SDK trace payloads through the existing WAJE audit authority."""

    def __init__(
        self,
        store: Any,
        *,
        policy: AgentTraceStoragePolicy = DEFAULT_AGENT_TRACE_STORAGE_POLICY,
    ) -> None:
        self._store = store
        self._policy = policy
        self._lock = threading.Lock()
        self._run_record_counts: dict[str, int] = {}

    def write_trace_record(self, record: Mapping[str, Any]) -> None:
        payload = dict(record)
        metadata = payload.get("waje_trace_metadata")
        routing = dict(metadata) if isinstance(metadata, Mapping) else {}
        run_id = str(routing.get("waje_run_id") or "")
        thread_id = str(routing.get("waje_thread_id") or "")
        if not run_id or not thread_id:
            raise AgentTraceStorageError("agent_trace_storage_identity_missing")
        try:
            record_bytes = len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise AgentTraceStorageError("agent_trace_storage_payload_invalid") from exc
        with self._lock:
            next_count = self._run_record_counts.get(run_id, 0) + 1
            rejection_code = (
                "agent_trace_storage_record_too_large"
                if record_bytes > self._policy.max_record_bytes
                else "agent_trace_storage_run_record_limit_exceeded"
                if next_count > self._policy.max_records_per_run
                else None
            )
            if rejection_code is None:
                self._run_record_counts[run_id] = next_count
        if rejection_code is not None:
            self._store.add_audit_event(
                "agents_sdk_trace_record_rejected",
                thread_id=thread_id,
                topic_id=str(routing.get("waje_topic_id") or ""),
                run_id=run_id,
                ref=str(payload.get("trace_id") or payload.get("id") or ""),
                payload={
                    "error_code": rejection_code,
                    "record_bytes": record_bytes,
                    "record_count": next_count,
                    "max_record_bytes": self._policy.max_record_bytes,
                    "max_records_per_run": self._policy.max_records_per_run,
                },
            )
            raise AgentTraceStorageError(rejection_code)
        self._store.add_audit_event(
            "agents_sdk_trace_recorded",
            thread_id=thread_id,
            topic_id=str(routing.get("waje_topic_id") or ""),
            run_id=run_id,
            ref=str(payload.get("trace_id") or payload.get("id") or ""),
            payload=payload,
        )


class WajeTraceProcessor(TracingProcessor):
    """Replaces the SDK exporter and writes trace data only to a WAJE sink."""

    def __init__(self, sink: AgentTraceSink | None = None) -> None:
        self._default_sink = sink
        self._lock = threading.Lock()
        self._trace_metadata: dict[str, dict[str, Any]] = {}
        self._run_sinks: dict[str, AgentTraceSink] = {}
        self._trace_sinks: dict[str, AgentTraceSink] = {}
        self._run_failures: dict[str, tuple[str, str]] = {}

    def register_run(self, run_id: str, sink: AgentTraceSink) -> None:
        if not run_id or run_id != run_id.strip():
            raise ValueError("agent_trace_run_id_invalid")
        with self._lock:
            existing = self._run_sinks.get(run_id)
            if existing is not None and existing is not sink:
                raise RuntimeError("agent_trace_run_sink_conflict")
            self._run_sinks[run_id] = sink
            self._run_failures.pop(run_id, None)

    def finish_run(self, run_id: str) -> tuple[str, str] | None:
        with self._lock:
            self._run_sinks.pop(run_id, None)
            return self._run_failures.pop(run_id, None)

    def on_trace_start(self, trace: Any) -> None:
        self._write("trace_started", trace)

    def on_trace_end(self, trace: Any) -> None:
        self._write("trace_finished", trace)

    def on_span_start(self, span: Any) -> None:
        self._write("span_started", span)

    def on_span_end(self, span: Any) -> None:
        self._write("span_finished", span)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None

    def _write(self, event_type: str, item: Any) -> None:
        run_id = ""
        trace_id = ""
        try:
            exported = item.export()
            payload = dict(exported) if isinstance(exported, Mapping) else {}
            trace_id = str(
                payload.get("id")
                if payload.get("object") == "trace"
                else payload.get("trace_id") or ""
            )
            if event_type == "trace_started":
                metadata = payload.get("metadata")
                if trace_id and isinstance(metadata, Mapping):
                    with self._lock:
                        self._trace_metadata[trace_id] = dict(metadata)
            with self._lock:
                routing = deepcopy(self._trace_metadata.get(trace_id, {}))
            run_id = str(routing.get("waje_run_id") or "")
            with self._lock:
                sink = (
                    self._trace_sinks.get(trace_id)
                    or self._run_sinks.get(run_id)
                    or self._default_sink
                )
                if trace_id and sink is not None:
                    self._trace_sinks[trace_id] = sink
            if sink is None:
                raise RuntimeError("agent_trace_sink_missing")
            payload["waje_trace_metadata"] = routing
            payload.update(
                {
                    "schema_version": "waje-agent-trace.v1",
                    "event_type": event_type,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            sink.write_trace_record(payload)
            if event_type == "trace_finished" and trace_id:
                with self._lock:
                    self._trace_metadata.pop(trace_id, None)
                    self._trace_sinks.pop(trace_id, None)
        except Exception as exc:
            failure_key = run_id or trace_id or "unbound"
            with self._lock:
                self._run_failures[failure_key] = (
                    event_type,
                    type(exc).__name__,
                )


def install_waje_trace_processor(sink: AgentTraceSink) -> WajeTraceProcessor:
    del sink
    global _WAJE_TRACE_PROCESSOR
    with _TRACE_INSTALL_LOCK:
        if _WAJE_TRACE_PROCESSOR is None:
            _WAJE_TRACE_PROCESSOR = WajeTraceProcessor()
            set_trace_processors([_WAJE_TRACE_PROCESSOR])
        else:
            provider = get_trace_provider()
            multi = getattr(provider, "_multi_processor", None)
            processors = tuple(getattr(multi, "_processors", ()))
            if processors != (_WAJE_TRACE_PROCESSOR,):
                raise RuntimeError("waje_trace_processor_configuration_conflict")
        return _WAJE_TRACE_PROCESSOR


def waje_trace_installation_state() -> Mapping[str, Any]:
    provider = get_trace_provider()
    multi = getattr(provider, "_multi_processor", None)
    processors = tuple(getattr(multi, "_processors", ()))
    with _TRACE_INSTALL_LOCK:
        installed = _WAJE_TRACE_PROCESSOR
    return {
        "exclusive_waje_processor": (
            installed is not None
            and len(processors) == 1
            and processors[0] is installed
        ),
        "processor_types": [type(processor).__name__ for processor in processors],
        "processor_count": len(processors),
    }


_TRACE_INSTALL_LOCK = threading.Lock()
_WAJE_TRACE_PROCESSOR: WajeTraceProcessor | None = None
