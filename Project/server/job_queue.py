"""The worker queue and the pool that drains it.

Two separate concerns are deliberately kept apart:

* the **queue** is bounded, so a burst of submissions cannot exhaust memory;
* the **pool** runs at most ``--workers`` jobs at once, because two concurrent
  NVENC encodes fight over the same fixed-function encoder and both get slower.
  Everything beyond that waits in the queue, and the client is told its position.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

LOG = logging.getLogger("rdo.queue")


class JobQueue:
    """Thread-safe bookkeeping for queued, running and finished jobs."""

    def __init__(self, maxsize: int = 64) -> None:
        self.pending: queue.Queue = queue.Queue(maxsize=maxsize)
        self.lock = threading.Lock()
        self.active: set[str] = set()
        self.waiting: list[str] = []
        self.cancelled: set[str] = set()
        self.results: dict[str, dict] = {}

    # -- queue bookkeeping -------------------------------------------------
    def position(self, job_id: str) -> int:
        with self.lock:
            if job_id in self.active:
                return 0
            return self.waiting.index(job_id) + 1 if job_id in self.waiting else 0

    def depth(self) -> int:
        with self.lock:
            return len(self.waiting)

    def active_count(self) -> int:
        with self.lock:
            return len(self.active)

    def begin(self, job_id: str) -> None:
        with self.lock:
            if job_id in self.waiting:
                self.waiting.remove(job_id)
            self.active.add(job_id)

    def finish(self, job_id: str) -> None:
        with self.lock:
            self.active.discard(job_id)
            if job_id in self.waiting:
                self.waiting.remove(job_id)

    def record(self, job_id: str, payload: dict) -> None:
        with self.lock:
            self.results[job_id] = payload

    def result(self, job_id: str) -> dict | None:
        with self.lock:
            return self.results.get(job_id)

    # -- cancellation ------------------------------------------------------
    def cancel(self, job_id: str) -> bool:
        """Cancel a job that has not started yet. ``False`` if already running."""
        with self.lock:
            if job_id in self.waiting:
                self.waiting.remove(job_id)
                self.cancelled.add(job_id)
                return True
            return False

    def take_cancel(self, job_id: str) -> bool:
        with self.lock:
            if job_id in self.cancelled:
                self.cancelled.discard(job_id)
                return True
            return False

    def __len__(self) -> int:
        return self.pending.qsize()


class JobPool:
    """Fixed-size thread pool that pulls identifiers off a :class:`JobQueue`."""

    def __init__(self, jobs: JobQueue, workers: int) -> None:
        self.jobs = jobs
        self.workers = max(1, workers)
        self._pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="JobWorker"
        )
        self._stop = threading.Event()

    def start(self) -> None:
        for index in range(self.workers):
            threading.Thread(
                target=self._loop, name=f"JobDispatch-{index + 1}", daemon=True
            ).start()
        LOG.info("job pool started with %d worker(s)", self.workers)

    def submit(self, job_id: str, runner: Callable[[str], None]) -> int:
        self.jobs.pending.put((job_id, runner))
        with self.jobs.lock:
            return 0 if job_id in self.jobs.active else len(self.jobs.waiting)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id, runner = self.jobs.pending.get(timeout=0.4)
            except queue.Empty:
                continue
            with self.jobs.lock:
                self.jobs.waiting.append(job_id)
            try:
                if self.jobs.take_cancel(job_id):
                    self.jobs.finish(job_id)
                    continue
                self._pool.submit(self._wrap, runner, job_id)
            except RuntimeError:
                self.jobs.finish(job_id)
            finally:
                self.jobs.pending.task_done()

    def _wrap(self, runner: Callable[[str], None], job_id: str) -> None:
        """Mark the job running only once a worker thread actually starts it.

        Doing this at dispatch time instead would make a job look "active" while
        it was still sitting in the executor's backlog, and cancelling it would
        then silently do nothing. The cancel flag is therefore re-checked here as
        well, which closes the window between dispatch and start.
        """
        if self.jobs.take_cancel(job_id):
            self.jobs.finish(job_id)
            return
        self.jobs.begin(job_id)
        try:
            runner(job_id)
        finally:
            self.jobs.finish(job_id)

    def shutdown(self, wait: bool = False, drain_timeout: float = 3.0) -> None:
        self._stop.set()
        deadline = time.perf_counter() + drain_timeout
        while self.jobs.active_count() and time.perf_counter() < deadline:
            time.sleep(0.1)
        self._pool.shutdown(wait=wait, cancel_futures=True)
        LOG.info("job pool stopped")


__all__ = ["JobQueue", "JobPool"]