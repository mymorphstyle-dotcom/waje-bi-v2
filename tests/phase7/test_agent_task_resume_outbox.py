from __future__ import annotations

from pathlib import Path
import time

from bi_agent.runtime.agent_task_resume_outbox import (
    AgentTaskResumeEnvelope,
    AgentTaskResumeLeaseLost,
    process_agent_task_resume_outbox,
)


ROOT = Path(__file__).resolve().parents[2]


class FakeOutbox:
    def __init__(self) -> None:
        self.envelopes = (
            AgentTaskResumeEnvelope(
                resume_ref="agent-task-resume:run-1",
                thread_id="thread-1",
                task_ref="run-1",
                attempt_count=1,
                lease_owner_id="worker-1",
                lease_epoch=1,
            ),
            AgentTaskResumeEnvelope(
                resume_ref="agent-task-resume:run-2",
                thread_id="thread-2",
                task_ref="run-2",
                attempt_count=2,
                lease_owner_id="worker-1",
                lease_epoch=1,
            ),
        )
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.heartbeats: list[str] = []

    def enqueue_ready(self, *, limit: int = 100):
        assert limit == 5
        return ("agent-task-resume:run-1", "agent-task-resume:run-2")

    def sweep_exhausted(self, *, limit: int = 100, max_attempts: int = 5):
        assert limit == 5
        assert max_attempts == 5
        return ()

    def claim_ready(
        self,
        *,
        limit: int = 100,
        lease_owner_id: str,
        max_attempts: int = 5,
        lease_seconds: int = 900,
    ):
        assert limit == 5
        assert lease_owner_id == "worker-1"
        assert max_attempts == 5
        assert lease_seconds > 0
        return self.envelopes

    def heartbeat(
        self,
        envelope: AgentTaskResumeEnvelope,
        *,
        lease_seconds: int,
    ) -> None:
        assert lease_seconds > 0
        self.heartbeats.append(envelope.task_ref)

    def complete(self, envelope: AgentTaskResumeEnvelope) -> None:
        self.completed.append(envelope.task_ref)

    def fail(
        self,
        envelope: AgentTaskResumeEnvelope,
        *,
        error_code: str,
        max_attempts: int = 5,
    ) -> str:
        self.failed.append((envelope.task_ref, error_code))
        return "exhausted" if envelope.attempt_count >= max_attempts else "failed"


def test_resume_outbox_completes_and_retries_with_stable_task_identity() -> None:
    outbox = FakeOutbox()

    def resume(thread_id: str, task_ref: str):
        assert thread_id == f"thread-{task_ref[-1]}"
        if task_ref == "run-2":
            raise RuntimeError("provider temporarily unavailable")
        return {"status": "completed", "task_ref": task_ref}

    result = process_agent_task_resume_outbox(
        outbox=outbox,  # type: ignore[arg-type]
        resume_runner=resume,
        limit=5,
        worker_id="worker-1",
    )

    assert result["enqueued"] == [
        "agent-task-resume:run-1",
        "agent-task-resume:run-2",
    ]
    assert result["claimed"] == ["run-1", "run-2"]
    assert outbox.completed == ["run-1"]
    assert outbox.failed == [("run-2", "RuntimeError")]
    assert result["superseded"] == []
    assert result["exhausted"] == []


def test_resume_outbox_fencing_prevents_a_stale_worker_from_overwriting() -> None:
    class LeaseLostOutbox(FakeOutbox):
        def __init__(self) -> None:
            super().__init__()
            self.envelopes = self.envelopes[:1]

        def complete(self, envelope: AgentTaskResumeEnvelope) -> None:
            raise AgentTaskResumeLeaseLost("lease_lost")

        def fail(
            self,
            envelope: AgentTaskResumeEnvelope,
            *,
            error_code: str,
            max_attempts: int = 5,
        ) -> str:
            raise AssertionError("stale worker must not update the reclaimed row")

    outbox = LeaseLostOutbox()
    result = process_agent_task_resume_outbox(
        outbox=outbox,  # type: ignore[arg-type]
        resume_runner=lambda _thread_id, _task_ref: {"status": "completed"},
        limit=5,
        worker_id="worker-1",
    )

    assert result["completed"] == []
    assert result["failed"] == []
    assert result["superseded"] == ["run-1"]


def test_resume_outbox_heartbeats_while_runner_is_active() -> None:
    outbox = FakeOutbox()
    outbox.envelopes = outbox.envelopes[:1]

    result = process_agent_task_resume_outbox(
        outbox=outbox,  # type: ignore[arg-type]
        resume_runner=lambda _thread_id, _task_ref: (
            time.sleep(0.04) or {"status": "completed"}
        ),
        limit=5,
        worker_id="worker-1",
        lease_seconds=1,
        heartbeat_interval_seconds=0.01,
    )

    assert result["completed"] == [{"task_ref": "run-1", "status": "completed"}]
    assert outbox.heartbeats


def test_resume_outbox_records_exhausted_terminal_state() -> None:
    outbox = FakeOutbox()
    outbox.envelopes = (
        AgentTaskResumeEnvelope(
            resume_ref="agent-task-resume:run-5",
            thread_id="thread-5",
            task_ref="run-5",
            attempt_count=5,
            lease_owner_id="worker-1",
            lease_epoch=5,
        ),
    )

    result = process_agent_task_resume_outbox(
        outbox=outbox,  # type: ignore[arg-type]
        resume_runner=lambda _thread_id, _task_ref: (_ for _ in ()).throw(
            RuntimeError("still unavailable")
        ),
        limit=5,
        worker_id="worker-1",
    )

    assert result["exhausted"] == ["agent-task-resume:run-5"]
    assert outbox.failed == [("run-5", "RuntimeError")]


def test_resume_outbox_is_discovered_from_terminal_bi_authority_and_checkpoint() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )
    module = (ROOT / "bi_agent/runtime/agent_task_resume_outbox.py").read_text(
        encoding="utf-8"
    )
    worker = (ROOT / "tools/runtime/recover_run_dispatches.py").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS waje_runtime.agent_task_resume_outbox" in schema
    assert "UNIQUE(thread_id, task_ref)" in schema
    assert "run.status IN ('completed', 'failed')" in module
    assert "checkpointKind" in module
    assert "awaitedTaskRef" in module
    assert "LIKE 'checkpoint:%%'" in module
    assert "FOR UPDATE SKIP LOCKED" in module
    assert "lease_expires_at <= now()" in module
    assert "lease_epoch = lease_epoch + 1" in module
    assert "def heartbeat(" in module
    assert "outbox_state = 'exhausted'" in module
    assert "exhausted_at" in schema
    assert "'exhausted'" in schema
    assert "lease_owner_id" in schema
    assert "lease_expires_at" in schema
    assert "process_agent_task_resume_outbox" in worker
