"""Centralized logging configuration."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    """Configure console and daily rotating file logs."""

    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        logs_dir / "telegram_to_x.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("telethon.network").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
