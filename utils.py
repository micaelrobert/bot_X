"""Small reusable helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


SAFE_PATH_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def safe_path_component(value: str) -> str:
    """Convert an arbitrary identifier into a safe short directory name."""

    sanitized = SAFE_PATH_RE.sub("_", value).strip("._")
    if len(sanitized) <= 80:
        return sanitized or hashlib.sha256(value.encode()).hexdigest()[:16]
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{sanitized[:60]}_{digest}"


def ensure_absolute_paths(paths: list[Path]) -> list[str]:
    """Return resolved string paths for Playwright file upload."""

    return [str(path.resolve()) for path in paths]
