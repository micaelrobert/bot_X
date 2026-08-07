from __future__ import annotations

import unittest
from types import SimpleNamespace

from media import MediaManager


class MediaCandidateTests(unittest.TestCase):
    def test_direct_photo_is_detected(self) -> None:
        message = SimpleNamespace(
            id=1,
            photo=object(),
            video=None,
            document=None,
            web_preview=None,
        )
        candidates = MediaManager._collect_candidates([message])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].kind, "image")

    def test_image_document_is_detected(self) -> None:
        document = SimpleNamespace(mime_type="image/jpeg")
        message = SimpleNamespace(
            id=2,
            photo=None,
            video=None,
            document=document,
            web_preview=None,
        )
        candidates = MediaManager._collect_candidates([message])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].kind, "image")

    def test_link_preview_photo_is_detected(self) -> None:
        preview_photo = object()
        preview = SimpleNamespace(photo=preview_photo, document=None, url="https://example.com")
        message = SimpleNamespace(
            id=3,
            photo=None,
            video=None,
            document=None,
            web_preview=preview,
        )
        candidates = MediaManager._collect_candidates([message])
        self.assertEqual(len(candidates), 1)
        self.assertIs(candidates[0].source, message)
        self.assertEqual(candidates[0].kind, "image")

    def test_extract_open_graph_image(self) -> None:
        document = '<meta property="og:image" content="/image.jpg">'
        self.assertEqual(
            MediaManager._extract_open_graph_image(
                document, "https://example.com/product/1"
            ),
            "https://example.com/image.jpg",
        )


if __name__ == "__main__":
    unittest.main()
