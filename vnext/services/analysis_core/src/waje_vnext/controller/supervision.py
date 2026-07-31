"""Periodic heartbeat supervision for long-running durable jobs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from threading import Event, Lock, Thread

from waje_vnext.domain.async_runtime import JobLease
from waje_vnext.storage.ports import AuthorityStore, LeaseFenceLost


class JobHeartbeatSupervisor:
    """Renew one delivery lease while provider or capability work is running."""

    def __init__(
        self,
        *,
        store: AuthorityStore,
        lease: JobLease,
        clock: Callable[[], datetime],
        lease_duration: timedelta,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._store = store
        self._lease = lease
        self._clock = clock
        self._lease_duration = lease_duration
        self._interval_seconds = max(
            0.01,
            min(60.0, lease_duration.total_seconds() / 3.0),
        )
        self._stop = Event()
        self._lock = Lock()
        self._failure: BaseException | None = None
        self._thread = Thread(
            target=self._run,
            name="waje-job-heartbeat-{}".format(
                lease.outbox_message_id
            ),
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop_and_get(self) -> JobLease:
        self._stop.set()
        self._thread.join()
        with self._lock:
            failure = self._failure
            lease = self._lease
        if failure is not None:
            raise LeaseFenceLost(
                "periodic job heartbeat failed"
            ) from failure
        return lease

    @property
    def current_lease(self) -> JobLease:
        with self._lock:
            return self._lease

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                heartbeat_at = self._clock()
                if (
                    heartbeat_at.tzinfo is None
                    or heartbeat_at.utcoffset() is None
                ):
                    raise ValueError(
                        "heartbeat clock must return timezone-aware time"
                    )
                with self._lock:
                    current = self._lease
                renewed = self._store.heartbeat_job_lease(
                    current,
                    heartbeat_at=heartbeat_at,
                    expires_at=heartbeat_at + self._lease_duration,
                )
                with self._lock:
                    self._lease = renewed
            except BaseException as error:
                with self._lock:
                    self._failure = error
                self._stop.set()
                return
