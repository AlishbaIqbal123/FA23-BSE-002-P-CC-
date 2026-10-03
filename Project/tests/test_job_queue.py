"""Job queue accounting, cancellation and worker-pool behaviour."""

from __future__ import annotations

import threading
import time

from server.job_queue import JobPool, JobQueue


class TestJobQueue:
    def test_starts_empty(self):
        jobs = JobQueue()
        assert jobs.depth() == 0
        assert jobs.active_count() == 0

    def test_begin_moves_a_job_from_waiting_to_active(self):
        jobs = JobQueue()
        jobs.waiting.append("a")
        jobs.begin("a")
        assert jobs.depth() == 0
        assert jobs.active_count() == 1
        assert jobs.position("a") == 0

    def test_position_counts_the_jobs_ahead(self):
        jobs = JobQueue()
        jobs.waiting.extend(["a", "b", "c"])
        assert jobs.position("c") == 3
        assert jobs.position("zzz") == 0

    def test_finish_clears_both_sets(self):
        jobs = JobQueue()
        jobs.active.add("a")
        jobs.finish("a")
        assert jobs.active_count() == 0

    def test_results_are_recorded_and_retrievable(self):
        jobs = JobQueue()
        jobs.record("j1", {"status": "ok"})
        assert jobs.result("j1") == {"status": "ok"}
        assert jobs.result("missing") is None

    def test_a_waiting_job_can_be_cancelled(self):
        jobs = JobQueue()
        jobs.waiting.append("a")
        assert jobs.cancel("a") is True
        assert jobs.depth() == 0
        assert jobs.take_cancel("a") is True
        assert jobs.take_cancel("a") is False

    def test_a_running_job_cannot_be_cancelled_through_the_queue(self):
        jobs = JobQueue()
        jobs.active.add("a")
        assert jobs.cancel("a") is False

    def test_concurrent_marking_is_safe(self):
        jobs = JobQueue()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()
            for step in range(200):
                jobs.begin(f"j{index}-{step}")
                jobs.finish(f"j{index}-{step}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert jobs.active_count() == 0
        assert jobs.depth() == 0


class TestJobPool:
    def test_a_submitted_job_runs_on_the_pool(self):
        jobs = JobQueue()
        pool = JobPool(jobs, workers=2)
        pool.start()
        done = threading.Event()
        seen: list[str] = []

        def runner(job_id: str) -> None:
            seen.append(job_id)
            jobs.finish(job_id)
            done.set()

        pool.submit("j1", runner)
        assert done.wait(5.0)
        pool.shutdown()
        assert seen == ["j1"]

    def test_worker_limit_is_respected(self):
        jobs = JobQueue()
        pool = JobPool(jobs, workers=2)
        pool.start()
        concurrent = 0
        peak = 0
        lock = threading.Lock()
        gate = threading.Event()

        def runner(job_id: str) -> None:
            nonlocal concurrent, peak
            with lock:
                concurrent += 1
                peak = max(peak, concurrent)
            gate.wait(3.0)
            with lock:
                concurrent -= 1
            jobs.finish(job_id)

        for index in range(6):
            pool.submit(f"j{index}", runner)
        time.sleep(0.5)
        gate.set()
        pool.shutdown(wait=True)
        assert peak <= 2

    def test_a_cancelled_job_never_runs(self):
        jobs = JobQueue()
        pool = JobPool(jobs, workers=1)
        pool.start()
        ran: list[str] = []
        blocker = threading.Event()

        def first(job_id: str) -> None:
            blocker.wait(2.0)
            jobs.finish(job_id)

        def second(job_id: str) -> None:
            ran.append(job_id)
            jobs.finish(job_id)

        pool.submit("first", first)
        time.sleep(0.3)
        pool.submit("second", second)
        time.sleep(0.2)
        assert jobs.cancel("second") is True
        blocker.set()
        pool.shutdown(wait=True)
        assert ran == []

    def test_shutdown_is_idempotent(self):
        jobs = JobQueue()
        pool = JobPool(jobs, workers=1)
        pool.start()
        pool.shutdown()
        pool.shutdown()