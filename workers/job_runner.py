"""
job_runner.py

A single background worker thread that executes jobs from a FIFO queue: pool
refresh scans, sync executions, remove executions, usage polls. Because there
is exactly one worker, the pool has a single writer by construction and needs
no locking.

Jobs are plain callables ``fn(ctx: JobContext) -> object``. The context lets a
job report byte/item progress and check for cancellation between units of work.
Progress and completion are delivered as Qt signals, which cross the
thread boundary via automatic queued connections and therefore land on the GUI
thread.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

logger = logging.getLogger(__name__)


class CancelToken:
    """A cooperative cancellation flag (checked between files / multipart parts)."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass
class Job:
    kind: str  # "refresh" | "sync" | "remove" | "usage" | "probe"
    fn: Callable[["JobContext"], Any]
    description: str = ""
    # Whether the queue-empty/idle signalling should reflect this as user-facing
    # work (usage polls are silent housekeeping).
    silent: bool = False
    meta: dict = field(default_factory=dict)


class JobContext:
    """Handed to a running job so it can report progress and observe cancel."""

    def __init__(self, runner: "JobRunner", cancel_token: CancelToken, job: Job) -> None:
        self._runner = runner
        self.cancel_token = cancel_token
        self.job = job

    @property
    def cancelled(self) -> bool:
        return self.cancel_token.cancelled

    def progress(self, done: int, total: int, message: str = "") -> None:
        self._runner.jobProgress.emit(int(done), int(total), message)

    def status(self, message: str) -> None:
        self._runner.jobStatus.emit(message)

    def log(self, message: str) -> None:
        self._runner.jobLog.emit(message)


class _WorkerThread(QThread):
    def __init__(self, runner: "JobRunner", job_queue: "queue.Queue[Optional[Job]]") -> None:
        super().__init__()
        self._runner = runner
        self._queue = job_queue
        self.current_token: Optional[CancelToken] = None
        self._lock = threading.Lock()

    def run(self) -> None:  # executes on the worker thread
        while True:
            job = self._queue.get()
            if job is None:  # sentinel: shut down
                return
            token = CancelToken()
            with self._lock:
                self.current_token = token
            ctx = JobContext(self._runner, token, job)
            self._runner.jobStarted.emit(job.kind, job.description)
            try:
                result = job.fn(ctx)
                self._runner.jobFinished.emit(job.kind, result)
            except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
                logger.exception("Job '%s' failed", job.kind)
                self._runner.jobFailed.emit(job.kind, str(exc))
            finally:
                with self._lock:
                    self.current_token = None
                if self._queue.empty():
                    self._runner.queueIdle.emit()

    def cancel_current(self) -> None:
        with self._lock:
            if self.current_token is not None:
                self.current_token.cancel()


class JobRunner(QObject):
    jobStarted = pyqtSignal(str, str)  # (kind, description)
    jobProgress = pyqtSignal(int, int, str)  # (done, total, message)
    jobStatus = pyqtSignal(str)  # one-line current operation
    jobLog = pyqtSignal(str)
    jobFinished = pyqtSignal(str, object)  # (kind, result)
    jobFailed = pyqtSignal(str, str)  # (kind, error)
    queueIdle = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._thread = _WorkerThread(self, self._queue)

    def start(self) -> None:
        if not self._thread.isRunning():
            self._thread.start()

    def submit(self, job: Job) -> None:
        self.start()
        self._queue.put(job)

    def cancel_current(self) -> None:
        self._thread.cancel_current()

    @property
    def busy(self) -> bool:
        return self._thread.current_token is not None or not self._queue.empty()

    def shutdown(self) -> None:
        """Cancel any running job and stop the worker thread (call on app exit)."""
        self._thread.cancel_current()
        self._queue.put(None)  # sentinel
        self._thread.wait(5000)
