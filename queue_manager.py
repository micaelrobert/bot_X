"""In-memory asyncio queue backed by a durable JSON outbox."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime

from models import PublishJob, PublishResult
from storage import PendingStore, PublishedStore

LOGGER = logging.getLogger(__name__)


class QueueManager:
    """Coordinate enqueueing, retries, acknowledgements and crash recovery."""

    def __init__(
        self,
        pending: PendingStore,
        published: PublishedStore,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> None:
        self.pending = pending
        self.published = published
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.queue: asyncio.Queue[PublishJob] = asyncio.Queue()
        self._runtime_jobs: set[str] = set()
        self._enqueue_lock = asyncio.Lock()
        self._retry_tasks: set[asyncio.Task[None]] = set()

    async def discard_pending(self) -> int:
        """Discard every durable job left by an earlier service execution."""

        async with self._enqueue_lock:
            discarded = await self.pending.clear()
            self._runtime_jobs.clear()
        if discarded:
            LOGGER.warning(
                "Pendências antigas descartadas no início | quantidade=%s",
                discarded,
            )
        return discarded

    async def recover(self) -> int:
        """Restore every durable pending job into the asyncio queue."""

        recovered = 0
        async with self._enqueue_lock:
            for job in await self.pending.list_jobs():
                if await self.published.contains_any(job.source_keys):
                    await self.pending.remove(job.job_id)
                    continue
                if job.job_id not in self._runtime_jobs:
                    self._runtime_jobs.add(job.job_id)
                    await self.queue.put(job)
                    recovered += 1
        if recovered:
            LOGGER.info("Fila restaurada | pendentes=%s", recovered)
        return recovered

    async def enqueue(self, job: PublishJob) -> bool:
        """Persist and enqueue a job unless it is published or already pending."""

        async with self._enqueue_lock:
            if await self.published.contains_any(job.source_keys):
                LOGGER.info("Mensagem já publicada; ignorando | job=%s", job.job_id)
                return False
            if (
                await self.pending.contains(job.job_id)
                or job.job_id in self._runtime_jobs
            ):
                LOGGER.info("Mensagem já está na fila; ignorando | job=%s", job.job_id)
                return False

            await self.pending.upsert(job)
            self._runtime_jobs.add(job.job_id)
            await self.queue.put(job)
        LOGGER.info("Mensagem adicionada à fila | job=%s", job.job_id)
        return True

    async def get(self) -> PublishJob:
        """Wait for and return the next job."""

        return await self.queue.get()

    def task_done(self) -> None:
        """Mark the current in-memory item as processed for this attempt."""

        self.queue.task_done()

    async def mark_publishing(self, job: PublishJob) -> None:
        """Persist the non-idempotent boundary immediately before clicking Post."""

        job.state = "publishing"
        job.last_attempt_at = datetime.now(UTC).isoformat()
        job.last_error = None
        await self.pending.upsert(job)

    async def mark_queued(self, job: PublishJob) -> None:
        """Return a recovered in-flight job to the normal queued state."""

        job.state = "queued"
        await self.pending.upsert(job)

    async def complete(self, job: PublishJob, result: PublishResult) -> None:
        """Atomically record success before removing the outbox entry."""

        await self.published.mark_published(job, result)
        await self.pending.remove(job.job_id)
        self._runtime_jobs.discard(job.job_id)

    async def fail(self, job: PublishJob, error: BaseException, ambiguous: bool) -> int:
        """Persist failure and schedule an exponential-backoff retry."""

        job.attempts += 1
        job.last_error = f"{type(error).__name__}: {error}"
        
        # Limite de tentativas para evitar retry infinito
        max_attempts = 5 if ambiguous else 10
        if job.attempts >= max_attempts:
            job.state = "failed"
            await self.pending.upsert(job)
            LOGGER.error(
                "Job esgotou tentativas máximas | job=%s | attempts=%s | ambiguous=%s | error=%s",
                job.job_id,
                job.attempts,
                ambiguous,
                job.last_error,
            )
            # Agendar remoção após delay
            exponential = self.retry_base_seconds * (2 ** min(job.attempts - 1, 8))
            delay = min(self.retry_max_seconds, exponential)
            delay = max(1, int(delay * random.uniform(0.85, 1.15)))
            task = asyncio.create_task(self._requeue_after(job, delay))
            self._retry_tasks.add(task)
            task.add_done_callback(self._retry_tasks.discard)
            return delay
        
        job.state = "publishing" if ambiguous else "queued"
        await self.pending.upsert(job)

        exponential = self.retry_base_seconds * (2 ** min(job.attempts - 1, 8))
        delay = min(self.retry_max_seconds, exponential)
        delay = max(1, int(delay * random.uniform(0.85, 1.15)))

        task = asyncio.create_task(self._requeue_after(job, delay))
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)
        return delay

    async def _requeue_after(self, job: PublishJob, delay: int) -> None:
        await asyncio.sleep(delay)
        # Se o job falhou definitivamente, não recolocar na fila
        if job.state != "failed":
            await self.queue.put(job)

    async def close(self) -> None:
        """Cancel retry timers; jobs remain safely persisted on disk."""

        for task in list(self._retry_tasks):
            task.cancel()
        if self._retry_tasks:
            await asyncio.gather(*self._retry_tasks, return_exceptions=True)
        self._retry_tasks.clear()
