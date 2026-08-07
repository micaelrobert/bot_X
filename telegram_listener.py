"""Telethon connection and new-message event handling."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl

from config import Settings
from models import PublishJob
from queue_manager import QueueManager
from text_utils import truncate_for_x

LOGGER = logging.getLogger(__name__)


class TelegramListener:
    """Listen only to messages arriving after the current service start."""

    def __init__(self, settings: Settings, queue: QueueManager) -> None:
        self.settings = settings
        self.queue = queue
        self.client = TelegramClient(
            str(settings.telegram_session_path),
            settings.api_id,
            settings.api_hash,
            auto_reconnect=True,
            connection_retries=None,
            retry_delay=settings.retry_base_seconds,
        )
        self.channel_entity: Any = None
        self._started_at = datetime.now(UTC)
        self._start_message_id = 0

    async def start(self) -> None:
        """Authenticate, resolve the configured channel and register handlers."""

        start_kwargs: dict[str, str] = {}
        if self.settings.telegram_phone:
            start_kwargs["phone"] = self.settings.telegram_phone
        if self.settings.telegram_2fa_password:
            start_kwargs["password"] = self.settings.telegram_2fa_password
        await self.client.start(**start_kwargs)
        channel_ref: str | int = self.settings.channel
        numeric_channel = self.settings.channel.lstrip("-").isdigit()
        if numeric_channel:
            channel_ref = int(self.settings.channel)
        try:
            self.channel_entity = await self.client.get_entity(channel_ref)
        except ValueError:
            if not numeric_channel:
                raise
            LOGGER.info(
                "Canal numérico ainda não estava no cache; carregando diálogos do Telegram"
            )
            await self.client.get_dialogs()
            self.channel_entity = await self.client.get_entity(channel_ref)
        latest_messages = await self.client.get_messages(
            self.channel_entity,
            limit=1,
        )
        self._start_message_id = (
            int(latest_messages[0].id) if latest_messages else 0
        )
        self._started_at = datetime.now(UTC)

        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=self.channel_entity),
        )
        self.client.add_event_handler(
            self._on_album,
            events.Album(chats=self.channel_entity),
        )

        me = await self.client.get_me()
        LOGGER.info(
            "Telegram conectado | conta=%s | canal=%s | início=%s | último_id_inicial=%s",
            getattr(me, "username", None) or getattr(me, "id", "desconhecido"),
            self.settings.channel,
            self._started_at.isoformat(),
            self._start_message_id,
        )

    async def wait_until_disconnected(self) -> None:
        """Block until Telethon disconnects."""

        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        """Disconnect Telethon without deleting the persisted session."""

        if self.client.is_connected():
            await self.client.disconnect()

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        message = event.message
        if getattr(message, "grouped_id", None) is not None:
            return
        if self._is_old(message):
            LOGGER.info("Mensagem anterior ao início ignorada | id=%s", message.id)
            return

        job = self._build_job([message], album_id=None)
        LOGGER.info(
            "Nova mensagem recebida | telegram_id=%s | job=%s",
            message.id,
            job.job_id,
        )
        await self.queue.enqueue(job)

    async def _on_album(self, event: events.Album.Event) -> None:
        messages = sorted(event.messages, key=lambda message: int(message.id))
        if not messages or self._is_old(messages[0]):
            LOGGER.info("Álbum anterior ao início ignorado")
            return

        album_id = getattr(messages[0], "grouped_id", None)
        job = self._build_job(messages, album_id=album_id)
        LOGGER.info(
            "Novo álbum recebido | telegram_ids=%s | job=%s",
            job.message_ids,
            job.job_id,
        )
        await self.queue.enqueue(job)

    def _is_old(self, message: Any) -> bool:
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id and message_id <= self._start_message_id:
            return True

        message_date = getattr(message, "date", None)
        if message_date is None:
            return False
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=UTC)
        cutoff = self._started_at - timedelta(
            seconds=self.settings.startup_grace_seconds
        )
        return message_date < cutoff

    def _build_job(self, messages: Iterable[Any], album_id: int | None) -> PublishJob:
        materialized = list(messages)
        chat_id = int(materialized[0].chat_id)
        message_ids = [int(message.id) for message in materialized]
        suffix = (
            f"album:{album_id}" if album_id is not None else f"message:{message_ids[0]}"
        )
        job_id = f"{chat_id}:{suffix}"
        raw_text = self._collect_text(materialized)
        text = truncate_for_x(raw_text, self.settings.x_char_limit)
        return PublishJob(
            job_id=job_id,
            chat_id=chat_id,
            channel_ref=self.settings.channel,
            message_ids=message_ids,
            text=text,
        )

    @staticmethod
    def _collect_text(messages: list[Any]) -> str:
        blocks: list[str] = []
        hidden_urls: list[str] = []

        for message in messages:
            raw_text = str(getattr(message, "raw_text", "") or "").strip()
            if raw_text and raw_text not in blocks:
                blocks.append(raw_text)

            for entity in getattr(message, "entities", None) or []:
                if isinstance(entity, MessageEntityTextUrl):
                    url = str(entity.url)
                    if url and url not in raw_text and url not in hidden_urls:
                        hidden_urls.append(url)

        text = "\n\n".join(blocks)
        if hidden_urls:
            text = f"{text}\n\n" if text else ""
            text += "\n".join(hidden_urls)
        return text
