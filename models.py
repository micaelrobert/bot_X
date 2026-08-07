"""Domain models shared by the listener, queue and publisher."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class PublishJob:
    """A durable unit of work representing one Telegram publication."""

    job_id: str
    chat_id: int
    channel_ref: str
    message_ids: list[int]
    text: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    attempts: int = 0
    state: str = "queued"
    last_error: str | None = None
    last_attempt_at: str | None = None

    @property
    def source_keys(self) -> list[str]:
        """Return stable per-message keys used for deduplication."""

        return [f"{self.chat_id}:{message_id}" for message_id in self.message_ids]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the job for JSON persistence."""

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublishJob":
        """Deserialize a persisted job."""

        return cls(
            job_id=str(data["job_id"]),
            chat_id=int(data["chat_id"]),
            channel_ref=str(data["channel_ref"]),
            message_ids=[int(value) for value in data["message_ids"]],
            text=str(data.get("text", "")),
            created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
            attempts=int(data.get("attempts", 0)),
            state=str(data.get("state", "queued")),
            last_error=data.get("last_error"),
            last_attempt_at=data.get("last_attempt_at"),
        )


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Result returned by an X publisher implementation."""

    x_id: str | None
    x_url: str | None
    reconciled: bool = False
