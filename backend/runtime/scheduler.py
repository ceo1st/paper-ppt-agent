"""Bounded async job scheduler.

The API accepts generation requests quickly, but actual work should pass
through one admission-control point. This scheduler keeps a small priority
queue, starts at most ``max_concurrent`` jobs, and lets endpoints return 429
instead of allowing unbounded local subprocesses, Agent sessions, and refine
tasks to pile up.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from backend.config import settings

logger = logging.getLogger(__name__)


class SchedulerDraining(RuntimeError):
    """Submitted after the runner started shutting down."""


class QueueFull(RuntimeError):
    """Raised when the bounded scheduler backlog is full."""


DEFAULT_PRIORITY = 10
JobRunner = Callable[[], Coroutine[Any, Any, None]]


@dataclass
class _QueuedJob:
    priority: int
    sequence: int
    job_id: str = field(compare=False)
    runner: JobRunner = field(compare=False)
    submit_ts: float = field(default_factory=time.monotonic, compare=False)
    cancelled: bool = field(default=False, compare=False)


@dataclass
class _RunningJob:
    job_id: str
    task: asyncio.Task[Any]
    submit_ts: float
    start_ts: float = field(default_factory=time.monotonic)
    priority: int = DEFAULT_PRIORITY


class Scheduler:
    """Priority scheduler with bounded queue and bounded running jobs."""

    def __init__(
        self,
        max_concurrent: int | None = None,
        queue_capacity: int | None = None,
    ) -> None:
        self._max_concurrent = max(1, int(max_concurrent or 1))
        self._queue_capacity = max(0, int(queue_capacity or 0))
        self._queued_heap: list[_QueuedJob] = []
        self._queued_by_id: dict[str, _QueuedJob] = {}
        self._running: dict[str, _RunningJob] = {}
        self._draining = False
        self._started = False
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._changed: asyncio.Event | None = None
        self._sequence = 0

    @property
    def queue_size(self) -> int:
        return len(self._queued_by_id)

    @property
    def running_count(self) -> int:
        return sum(1 for item in self._running.values() if not item.task.done())

    def diagnostics(self) -> dict[str, Any]:
        return {
            "mode": "bounded",
            "started": self._started,
            "draining": self._draining,
            "max_concurrent": self._max_concurrent,
            "queue_size": self.queue_size,
            "queue_capacity": self._queue_capacity,
            "queued_job_ids": [
                item.job_id
                for item in sorted(self._queued_by_id.values())
                if not item.cancelled
            ],
            "running_count": self.running_count,
            "running_job_ids": [
                job_id
                for job_id, item in self._running.items()
                if not item.task.done()
            ],
        }

    async def start(self) -> None:
        self._started = True
        self._draining = False
        if self._changed is None:
            self._changed = asyncio.Event()
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(
                self._dispatch_loop(),
                name="job-scheduler-dispatcher",
            )

    async def shutdown(self, timeout: float = 30.0) -> None:
        self._draining = True
        self._notify()
        deadline = time.monotonic() + timeout
        while self.running_count and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None

        for item in list(self._queued_by_id.values()):
            item.cancelled = True
        self._queued_by_id.clear()
        self._queued_heap.clear()

        for job_id, item in list(self._running.items()):
            if not item.task.done():
                logger.warning("scheduler: cancelling job %s on shutdown", job_id)
                item.task.cancel()

        for item in list(self._running.values()):
            try:
                await item.task
            except (asyncio.CancelledError, Exception):
                pass

        self._running.clear()
        self._started = False

    async def submit(
        self,
        job_id: str,
        runner: JobRunner,
        *,
        priority: int = DEFAULT_PRIORITY,
    ) -> int:
        """Queue a job and return its zero-based position.

        ``0`` means the job is eligible to start immediately, although the
        dispatcher still performs the actual task creation asynchronously.
        """
        if self._draining:
            raise SchedulerDraining("scheduler is shutting down")
        if not self._started:
            await self.start()

        if job_id in self._running and not self._running[job_id].task.done():
            raise RuntimeError(f"job {job_id} is already running")
        if job_id in self._queued_by_id:
            raise RuntimeError(f"job {job_id} is already queued")
        available_start_slots = max(0, self._max_concurrent - self.running_count)
        max_pending_before_dispatch = self._queue_capacity + available_start_slots
        if self.queue_size >= max_pending_before_dispatch:
            raise QueueFull("server job queue is full")

        self._sequence += 1
        item = _QueuedJob(
            priority=priority,
            sequence=self._sequence,
            job_id=job_id,
            runner=runner,
        )
        heapq.heappush(self._queued_heap, item)
        self._queued_by_id[job_id] = item
        position = self._queue_position(job_id)
        self._notify()
        return position

    def cancel(self, job_id: str) -> bool:
        queued = self._queued_by_id.pop(job_id, None)
        if queued is not None:
            queued.cancelled = True
            self._notify()
            return True

        item = self._running.get(job_id)
        if item is None or item.task.done():
            return False
        item.task.cancel()
        self._notify()
        return True

    def is_active(self, job_id: str) -> bool:
        item = self._running.get(job_id)
        if item and not item.task.done():
            return True
        queued = self._queued_by_id.get(job_id)
        return bool(queued and not queued.cancelled)

    async def _dispatch_loop(self) -> None:
        while True:
            await self._wait_for_dispatchable_work()
            if self._draining:
                return
            while self.running_count < self._max_concurrent:
                item = self._pop_next_queued()
                if item is None:
                    break
                task = asyncio.create_task(
                    self._run_one(item),
                    name=f"job-{item.job_id}",
                )
                self._running[item.job_id] = _RunningJob(
                    job_id=item.job_id,
                    task=task,
                    submit_ts=item.submit_ts,
                    priority=item.priority,
                )

    async def _wait_for_dispatchable_work(self) -> None:
        while not self._draining and (
            self.running_count >= self._max_concurrent or not self._queued_by_id
        ):
            if self._changed is None:
                self._changed = asyncio.Event()
            self._changed.clear()
            await self._changed.wait()

    def _pop_next_queued(self) -> _QueuedJob | None:
        while self._queued_heap:
            item = heapq.heappop(self._queued_heap)
            current = self._queued_by_id.get(item.job_id)
            if current is not item or item.cancelled:
                continue
            self._queued_by_id.pop(item.job_id, None)
            return item
        return None

    async def _run_one(self, item: _QueuedJob) -> None:
        started = time.monotonic()
        logger.info("scheduler: starting job %s", item.job_id)
        try:
            await item.runner()
        except asyncio.CancelledError:
            logger.info("scheduler: job %s cancelled", item.job_id)
        except Exception:
            logger.exception("scheduler: job %s raised unhandled exception", item.job_id)
        finally:
            elapsed = time.monotonic() - started
            logger.info("scheduler: job %s finished after %.1fs", item.job_id, elapsed)
            current = self._running.get(item.job_id)
            if current is not None and current.task is asyncio.current_task():
                self._running.pop(item.job_id, None)
            self._notify()

    def _queue_position(self, job_id: str) -> int:
        ordered = [
            item
            for item in sorted(self._queued_by_id.values())
            if not item.cancelled
        ]
        for index, item in enumerate(ordered):
            if item.job_id == job_id:
                return index
        return 0

    def _notify(self) -> None:
        if self._changed is not None:
            self._changed.set()


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(
            max_concurrent=settings.generation_worker_concurrency,
            queue_capacity=settings.job_queue_capacity,
        )
    return _scheduler
