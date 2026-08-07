from __future__ import annotations

import unittest

from models import PublishJob


class PublishJobTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        job = PublishJob(
            job_id="-100:message:10",
            chat_id=-100,
            channel_ref="@canal",
            message_ids=[10],
            text="teste",
        )
        restored = PublishJob.from_dict(job.to_dict())
        self.assertEqual(restored.job_id, job.job_id)
        self.assertEqual(restored.source_keys, ["-100:10"])


if __name__ == "__main__":
    unittest.main()
