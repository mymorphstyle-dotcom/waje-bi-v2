from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import threading
from typing import Any, Mapping

from agents.tracing import set_trace_processors
from agents.tracing.processor_interface import TracingProcessor

from bi_agent.runtime.agent_sdk_contracts import AgentTraceSink


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

    def __init__(self, store: Any) -> None:
        self._store = store

    def write_trace_record(self, record: Mapping[str, Any]) -> None:
        payload = dict(record)
        metadata = payload.get("waje_trace_metadata")
        routing = dict(metadata) if isinstance(metadata, Mapping) else {}
        self._store.add_audit_event(
            "agents_sdk_trace_recorded",
            thread_id=str(routing.get("waje_thread_id") or ""),
            topic_id=str(routing.get("waje_topic_id") or ""),
            run_id=str(routing.get("waje_run_id") or ""),
            ref=str(payload.get("trace_id") or payload.get("id") or ""),
            payload=payload,
        )


class WajeTraceProcessor(TracingProcessor):
    """Replaces the SDK exporter and writes trace data only to a WAJE sink."""

    def __init__(self, sink: AgentTraceSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._trace_metadata: dict[str, dict[str, Any]] = {}

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
            payload["waje_trace_metadata"] = routing
            payload.update(
                {
                    "schema_version": "waje-agent-trace.v1",
                    "event_type": event_type,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._sink.write_trace_record(payload)
            if event_type == "trace_finished" and trace_id:
                with self._lock:
                    self._trace_metadata.pop(trace_id, None)
        except Exception:
            # Trace persistence cannot change the model/tool terminal. Store health is
            # supervised by AgentTurnRuntime and its own durable failure path.
            return None


def install_waje_trace_processor(sink: AgentTraceSink) -> WajeTraceProcessor:
    processor = WajeTraceProcessor(sink)
    set_trace_processors([processor])
    return processor
