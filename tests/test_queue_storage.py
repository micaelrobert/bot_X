from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from models import PublishJob, PublishResult
from queue_manager import QueueManager
from storage import PendingStore, PublishedStore


class QueueStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.pending = PendingStore(root / "pending.json")
        self.published = PublishedStore(root / "published.json")
        self.queue = QueueManager(self.pending, self.published, 1, 2)

    async def asyncTearDown(self) -> None:
        await self.queue.close()
        self.temp_dir.cleanup()

    async def test_pending_job_is_recovered_and_completed(self) -> None:
        job = PublishJob(
            job_id="-100:message:42",
            chat_id=-100,
            channel_ref="@canal",
            message_ids=[42],
            text="teste",
        )
        self.assertTrue(await self.queue.enqueue(job))
        queued = await self.queue.get()
        self.assertEqual(queued.job_id, job.job_id)
        self.queue.task_done()

        await self.queue.mark_publishing(job)
        await self.queue.complete(
            job, PublishResult("999", "https://x.com/u/status/999")
        )

        self.assertTrue(await self.published.contains_any(job.source_keys))
        self.assertFalse(await self.pending.contains(job.job_id))
        self.assertFalse(await self.queue.enqueue(job))

    async def test_recover_restores_durable_job(self) -> None:
        job = PublishJob(
            job_id="-100:message:77",
            chat_id=-100,
            channel_ref="@canal",
            message_ids=[77],
            text="pendente",
        )
        await self.pending.upsert(job)
        self.assertEqual(await self.queue.recover(), 1)
        restored = await self.queue.get()
        self.assertEqual(restored.message_ids, [77])
        self.queue.task_done()


if __name__ == "__main__":
    unittest.main()
