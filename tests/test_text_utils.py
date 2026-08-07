from __future__ import annotations

import unittest

from text_utils import normalize_text, truncate_for_x, weighted_length


class TextUtilsTests(unittest.TestCase):
    def test_normalize_preserves_paragraphs_and_emoji(self) -> None:
        text = "  Oferta   especial 🔥  \n\n\n Link:   https://example.com/a  "
        self.assertEqual(
            normalize_text(text),
            "Oferta especial 🔥\n\nLink: https://example.com/a",
        )

    def test_url_has_transformed_weight(self) -> None:
        self.assertEqual(weighted_length("https://example.com/very/long/path"), 23)

    def test_truncation_respects_limit(self) -> None:
        result = truncate_for_x("palavra " * 100, limit=80)
        self.assertLessEqual(weighted_length(result), 80)
        self.assertTrue(result.endswith("…"))

    def test_truncation_never_splits_a_url(self) -> None:
        url = "https://example.com/um/caminho/muito/grande?com=parametros"
        result = truncate_for_x(("texto " * 30) + url, limit=80)
        self.assertIn(url, result)


if __name__ == "__main__":
    unittest.main()
