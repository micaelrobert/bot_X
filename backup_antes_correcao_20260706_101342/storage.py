"""Atomic JSON persistence for published IDs and the durable outbox."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from models import PublishJob, PublishResult


class PersistenceError(RuntimeError):
    """Raised when a persistence file cannot be read safely."""


class JsonFile:
    """Small atomic JSON file helper guarded by an asyncio lock."""

    def __init__(self, path: Path, default: dict[str, Any]) -> None:
        self.path = path
        self.default = default
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_sync(default)

    def _read_sync(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                f"Não foi possível ler o arquivo de persistência: {self.path}"
            ) from exc
        if not isinstance(data, dict):
            raise PersistenceError(
                f"O arquivo de persistência não contém um objeto JSON: {self.path}"
            )
        return data

    def _write_sync(self, data: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, self.path)

    async def read(self) -> dict[str, Any]:
        """Read the entire JSON object."""

        async with self._lock:
            return self._read_sync()

    async def mutate(self, callback: Any) -> Any:
        """Atomically load, mutate and rewrite the JSON object."""

        async with self._lock:
            data = self._read_sync()
            result = callback(data)
            self._write_sync(data)
            return result


class PublishedStore:
    """Persistent record of Telegram messages that reached X."""

    def __init__(self, path: Path) -> None:
        self.file = JsonFile(path, {"version": 1, "items": {}})

    async def contains_any(self, source_keys: list[str]) -> bool:
        """Return true when at least one source key is already recorded."""

        data = await self.file.read()
        items = data.setdefault("items", {})
        return any(key in items for key in source_keys)

    async def mark_published(self, job: PublishJob, result: PublishResult) -> None:
        """Record every Telegram message ID from a successful publication."""

        def mutate(data: dict[str, Any]) -> None:
            items = data.setdefault("items", {})
            payload = {
                "job_id": job.job_id,
                "x_id": result.x_id,
                "x_url": result.x_url,
                "published_at": job.last_attempt_at,
                "reconciled": result.reconciled,
            }
            for key in job.source_keys:
                items[key] = payload

        await self.file.mutate(mutate)


class PendingStore:
    """Durable outbox storing jobs until they are acknowledged."""

    def __init__(self, path: Path) -> None:
        self.file = JsonFile(path, {"version": 1, "jobs": {}})

    async def list_jobs(self) -> list[PublishJob]:
        """Load all pending jobs."""

        data = await self.file.read()
        jobs = data.setdefault("jobs", {})
        return [PublishJob.from_dict(raw) for raw in jobs.values()]

    async def contains(self, job_id: str) -> bool:
        """Return whether a job ID is already pending."""

        data = await self.file.read()
        return job_id in data.setdefault("jobs", {})

    async def upsert(self, job: PublishJob) -> None:
        """Insert or update a pending job."""

        def mutate(data: dict[str, Any]) -> None:
            data.setdefault("jobs", {})[job.job_id] = job.to_dict()

        await self.file.mutate(mutate)

    async def remove(self, job_id: str) -> None:
        """Delete an acknowledged job."""

        def mutate(data: dict[str, Any]) -> None:
            data.setdefault("jobs", {}).pop(job_id, None)

        await self.file.mutate(mutate)
