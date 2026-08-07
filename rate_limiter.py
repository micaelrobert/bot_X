"""Persistent publication spacing for X."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from storage import RateLimitStore

LOGGER = logging.getLogger(__name__)


class PublishRateLimiter:
    """Guarantee a minimum interval between publication attempts, across restarts."""

    def __init__(self, store: RateLimitStore, interval_seconds: int) -> None:
        self.store = store
        self.interval_seconds = interval_seconds
        self._lock = asyncio.Lock()

    async def wait_and_reserve(self) -> None:
        """Wait until a slot is available, then persist the next allowed slot."""

        if self.interval_seconds <= 0:
            return

        async with self._lock:
            next_allowed_at = await self._load_next_allowed_at()
            now = datetime.now(UTC)

            if next_allowed_at is not None and next_allowed_at > now:
                delay = (next_allowed_at - now).total_seconds()
                LOGGER.info(
                    "Aguardando intervalo mínimo entre publicações | espera=%.0fs",
                    delay,
                )
                await asyncio.sleep(delay)

            reserved_at = datetime.now(UTC)
            next_slot = reserved_at + timedelta(seconds=self.interval_seconds)
            await self.store.set_next_allowed_at(next_slot.isoformat())
            LOGGER.info(
                "Horário de publicação reservado | próximo_envio_após=%s",
                next_slot.isoformat(),
            )

    async def _load_next_allowed_at(self) -> datetime | None:
        raw = await self.store.get_next_allowed_at()
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            LOGGER.warning("Timestamp inválido no rate_limit.json; ignorando")
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
