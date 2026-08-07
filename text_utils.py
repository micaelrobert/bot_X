"""Text normalization and X-compatible weighted truncation."""

from __future__ import annotations

import re
import unicodedata

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
MULTISPACE_RE = re.compile(r"[ \t\f\v]+")
EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")

# Compatible with twitter-text v2 weighting for ordinary code points.
SINGLE_WEIGHT_RANGES = (
    (0, 4351),
    (8192, 8205),
    (8208, 8223),
    (8242, 8247),
)
TRANSFORMED_URL_LENGTH = 23


def normalize_text(text: str) -> str:
    """Remove accidental spacing while preserving emojis, links and paragraphs."""

    normalized = unicodedata.normalize("NFC", text or "")
    lines = [MULTISPACE_RE.sub(" ", line).strip() for line in normalized.splitlines()]
    normalized = "\n".join(lines)
    normalized = EXCESS_BLANK_LINES_RE.sub("\n\n", normalized)
    return normalized.strip()


def _char_weight(char: str) -> int:
    codepoint = ord(char)
    return (
        1
        if any(start <= codepoint <= end for start, end in SINGLE_WEIGHT_RANGES)
        else 2
    )


def weighted_length(text: str) -> int:
    """Estimate X's weighted character count, counting each URL as 23."""

    total = 0
    cursor = 0
    for match in URL_RE.finditer(text):
        total += sum(_char_weight(char) for char in text[cursor : match.start()])
        total += TRANSFORMED_URL_LENGTH
        cursor = match.end()
    total += sum(_char_weight(char) for char in text[cursor:])
    return total


def _truncate_plain(text: str, budget: int) -> str:
    """Truncate text that does not contain URLs to a weighted budget."""

    used = 0
    output: list[str] = []
    for char in text:
        char_weight = _char_weight(char)
        if used + char_weight > budget:
            break
        output.append(char)
        used += char_weight

    candidate = "".join(output).rstrip()
    boundary = max(candidate.rfind(" "), candidate.rfind("\n"))
    if boundary >= max(0, len(candidate) - 30):
        candidate = candidate[:boundary].rstrip(" ,.;:-")
    return candidate


def truncate_for_x(text: str, limit: int = 280, suffix: str = "…") -> str:
    """Truncate elegantly, preserving every URL that can fit as a whole token."""

    normalized = normalize_text(text)
    if weighted_length(normalized) <= limit:
        return normalized

    urls = [match.group(0) for match in URL_RE.finditer(normalized)]
    plain_text = normalize_text(URL_RE.sub(" ", normalized))
    suffix_weight = weighted_length(suffix)

    kept_urls: list[str] = []
    for url in urls:
        trial = "\n".join([*kept_urls, url])
        # Reserve at least the truncation suffix. The visible text is optional.
        if weighted_length(trial) + suffix_weight <= limit:
            kept_urls.append(url)
        else:
            break

    footer = "\n".join(kept_urls)
    separator = "\n\n" if footer and plain_text else ""
    reserved = suffix_weight + weighted_length(separator + footer)
    plain_budget = max(0, limit - reserved)
    candidate = _truncate_plain(plain_text, plain_budget)

    parts: list[str] = []
    if candidate:
        parts.append(f"{candidate}{suffix}")
    elif plain_text:
        parts.append(suffix)
    if footer:
        parts.append(footer)

    result = "\n\n".join(parts)
    while weighted_length(result) > limit and candidate:
        candidate = candidate[:-1].rstrip()
        parts[0] = f"{candidate}{suffix}" if candidate else suffix
        result = "\n\n".join(parts)
    return result
