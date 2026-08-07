from __future__ import annotations

import unittest

from x_publisher import PlaywrightXPublisher


class XPublisherTests(unittest.TestCase):
    def test_extract_created_tweet_id(self) -> None:
        payload = {
            "data": {
                "create_tweet": {
                    "tweet_results": {
                        "result": {
                            "__typename": "Tweet",
                            "rest_id": "123456789",
                        }
                    }
                }
            }
        }
        self.assertEqual(
            PlaywrightXPublisher._extract_created_post_id(payload),
            "123456789",
        )

    def test_extract_nested_created_post_id(self) -> None:
        payload = {
            "data": {
                "create_post": {
                    "post_results": {
                        "result": {
                            "post": {
                                "rest_id": "987654321",
                            }
                        }
                    }
                }
            }
        }
        self.assertEqual(
            PlaywrightXPublisher._extract_created_post_id(payload),
            "987654321",
        )

    def test_text_signature_removes_urls(self) -> None:
        signature = PlaywrightXPublisher._text_signature(
            "Oferta especial https://example.com/item"
        )
        self.assertEqual(signature, "oferta especial")


if __name__ == "__main__":
    unittest.main()
