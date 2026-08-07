"""Telegram media discovery, download and temporary-file cleanup."""

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import aiohttp

from models import PublishJob
from text_utils import URL_RE
from utils import safe_path_component

LOGGER = logging.getLogger(__name__)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image|twitter:image:src)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_IMAGE_RE_REVERSED = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image|twitter:image:src)["\']',
    re.IGNORECASE,
)
MAX_FALLBACK_IMAGE_BYTES = 20 * 1024 * 1024


class MediaDownloadError(RuntimeError):
    """Raised when expected Telegram media cannot be obtained safely."""


@dataclass(frozen=True, slots=True)
class _MediaCandidate:
    """One downloadable media object and its Telegram-message fallback."""

    source: Any
    fallback_message: Any
    kind: Literal["image", "video"]
    message_id: int
    origin: str


class MediaManager:
    """Download direct media, albums and images from Telegram link previews."""

    def __init__(self, media_root: Path, max_images: int = 4) -> None:
        self.media_root = media_root
        self.max_images = max_images
        self.media_root.mkdir(parents=True, exist_ok=True)

    async def download_for_job(
        self,
        client: Any,
        channel_entity: Any,
        job: PublishJob,
    ) -> list[Path]:
        """Fetch current Telegram messages and materialize media for one job.

        Telegram can initially expose a link preview as pending. For URL-bearing
        messages, this method re-fetches the message briefly before falling back to
        the page's Open Graph image. A job that is known to contain media is never
        silently degraded to text-only.
        """

        job_dir = self._job_dir(job)
        self.cleanup(job)
        job_dir.mkdir(parents=True, exist_ok=True)

        messages, candidates = await self._fetch_candidates_with_retry(
            client=client,
            channel_entity=channel_entity,
            job=job,
        )
        selected = self._select_candidates(candidates, job)

        paths: list[Path] = []
        errors: list[str] = []
        seen_paths: set[Path] = set()

        for candidate in selected:
            try:
                downloaded = await self._download_candidate(
                    client=client,
                    candidate=candidate,
                    job_dir=job_dir,
                )
            except Exception as exc:
                errors.append(
                    f"id={candidate.message_id} origem={candidate.origin}: "
                    f"{type(exc).__name__}: {exc}"
                )
                LOGGER.exception(
                    "Falha ao baixar mídia do Telegram | job=%s | telegram_id=%s | origem=%s",
                    job.job_id,
                    candidate.message_id,
                    candidate.origin,
                )
                continue

            path = self._validate_downloaded_path(downloaded)
            if path is None:
                errors.append(
                    f"id={candidate.message_id} origem={candidate.origin}: "
                    "arquivo ausente, vazio ou retorno inválido"
                )
                continue

            if path not in seen_paths:
                seen_paths.add(path)
                paths.append(path)
                LOGGER.info(
                    "Mídia baixada | job=%s | telegram_id=%s | origem=%s | arquivo=%s | bytes=%s",
                    job.job_id,
                    candidate.message_id,
                    candidate.origin,
                    path.name,
                    path.stat().st_size,
                )

        if not paths and URL_RE.search(job.text):
            try:
                fallback = await self._download_open_graph_image(job.text, job_dir)
            except Exception as exc:
                errors.append(f"fallback Open Graph: {type(exc).__name__}: {exc}")
                LOGGER.warning(
                    "Falha ao obter imagem Open Graph | job=%s",
                    job.job_id,
                    exc_info=True,
                )
            else:
                if fallback is not None:
                    paths.append(fallback)
                    LOGGER.info(
                        "Imagem Open Graph baixada | job=%s | arquivo=%s | bytes=%s",
                        job.job_id,
                        fallback.name,
                        fallback.stat().st_size,
                    )

        had_supported_media = bool(selected) or self._messages_indicate_media(messages)
        if had_supported_media and not paths:
            self.cleanup(job)
            details = "; ".join(errors) or "motivo não informado"
            raise MediaDownloadError(
                "A mensagem contém imagem/vídeo, mas nenhum arquivo pôde ser baixado. "
                f"Detalhes: {details}"
            )

        if not paths:
            LOGGER.info(
                "Nenhuma mídia encontrada para a mensagem | job=%s | ids=%s",
                job.job_id,
                job.message_ids,
            )
            self._remove_empty_dir(job_dir)
        return paths

    async def _fetch_candidates_with_retry(
        self,
        client: Any,
        channel_entity: Any,
        job: PublishJob,
    ) -> tuple[list[Any], list[_MediaCandidate]]:
        """Re-fetch messages while Telegram finishes building a link preview."""

        has_url = bool(URL_RE.search(job.text))
        delays = (0.0, 2.0, 4.0, 6.0) if has_url else (0.0,)
        last_messages: list[Any] = []

        for attempt, delay in enumerate(delays, start=1):
            if delay:
                LOGGER.info(
                    "Aguardando prévia de link do Telegram | job=%s | tentativa=%s | espera=%ss",
                    job.job_id,
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)

            last_messages = await self._fetch_messages(
                client,
                channel_entity,
                job.message_ids,
            )
            candidates = self._collect_candidates(last_messages)
            if candidates:
                LOGGER.info(
                    "Mídia detectada no Telegram | job=%s | candidatos=%s",
                    job.job_id,
                    [candidate.origin for candidate in candidates],
                )
                return last_messages, candidates

            if not has_url or not self._preview_may_be_pending(last_messages):
                break

        return last_messages, []

    @staticmethod
    async def _fetch_messages(
        client: Any,
        channel_entity: Any,
        message_ids: list[int],
    ) -> list[Any]:
        messages = await client.get_messages(channel_entity, ids=message_ids)
        if messages is None:
            return []
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        return sorted(
            [message for message in messages if message is not None],
            key=lambda message: int(getattr(message, "id", 0) or 0),
        )

    @classmethod
    def _collect_candidates(cls, messages: list[Any]) -> list[_MediaCandidate]:
        """Build candidates using Telethon's convenience media properties."""

        candidates: list[_MediaCandidate] = []
        dedupe: set[tuple[int, str]] = set()

        def add(
            *,
            source: Any,
            fallback_message: Any,
            kind: Literal["image", "video"],
            message_id: int,
            origin: str,
        ) -> None:
            if source is None:
                return
            key = (message_id, kind)
            if key in dedupe:
                return
            dedupe.add(key)
            candidates.append(
                _MediaCandidate(
                    source=source,
                    fallback_message=fallback_message,
                    kind=kind,
                    message_id=message_id,
                    origin=origin,
                )
            )

        for message in messages:
            message_id = int(getattr(message, "id", 0) or 0)
            direct_video = getattr(message, "video", None)
            direct_photo = getattr(message, "photo", None)
            document = getattr(message, "document", None)
            document_mime = cls._mime_type(document)
            web_preview = getattr(message, "web_preview", None)

            if direct_video is not None or document_mime.startswith("video/"):
                add(
                    source=message,
                    fallback_message=message,
                    kind="video",
                    message_id=message_id,
                    origin="vídeo direto ou da prévia",
                )
                continue

            if direct_photo is not None:
                # Telethon 1.44 also exposes a web-preview photo through .photo.
                add(
                    source=message,
                    fallback_message=message,
                    kind="image",
                    message_id=message_id,
                    origin=(
                        "imagem da prévia do link"
                        if web_preview is not None
                        else "foto direta"
                    ),
                )
                continue

            if document is not None and document_mime.startswith("image/"):
                add(
                    source=message,
                    fallback_message=message,
                    kind="image",
                    message_id=message_id,
                    origin=f"imagem enviada como arquivo ({document_mime})",
                )
                continue

            if web_preview is None:
                continue

            preview_photo = getattr(web_preview, "photo", None)
            preview_document = getattr(web_preview, "document", None)
            preview_mime = cls._mime_type(preview_document)

            if preview_photo is not None:
                add(
                    source=message,
                    fallback_message=message,
                    kind="image",
                    message_id=message_id,
                    origin="imagem da prévia do link",
                )
            elif preview_document is not None and preview_mime.startswith("image/"):
                add(
                    source=message,
                    fallback_message=message,
                    kind="image",
                    message_id=message_id,
                    origin=f"documento da prévia do link ({preview_mime})",
                )
            elif preview_document is not None and preview_mime.startswith("video/"):
                add(
                    source=message,
                    fallback_message=message,
                    kind="video",
                    message_id=message_id,
                    origin=f"vídeo da prévia do link ({preview_mime})",
                )

        return candidates

    def _select_candidates(
        self,
        candidates: list[_MediaCandidate],
        job: PublishJob,
    ) -> list[_MediaCandidate]:
        videos = [candidate for candidate in candidates if candidate.kind == "video"]
        images = [candidate for candidate in candidates if candidate.kind == "image"]

        if videos:
            if len(videos) > 1 or images:
                LOGGER.warning(
                    "X aceita apenas um vídeo por publicação; mídias excedentes foram ignoradas | job=%s",
                    job.job_id,
                )
            return [videos[0]]

        if len(images) > self.max_images:
            LOGGER.warning(
                "Álbum limitado a %s imagens para o X | job=%s",
                self.max_images,
                job.job_id,
            )
        return images[: self.max_images]

    async def _download_candidate(
        self,
        client: Any,
        candidate: _MediaCandidate,
        job_dir: Path,
    ) -> str | bytes | None:
        """Download via the complete message, which includes web previews."""

        try:
            return await client.download_media(candidate.source, file=str(job_dir))
        except Exception:
            if candidate.source is candidate.fallback_message:
                raise
            LOGGER.debug(
                "Download do objeto específico falhou; tentando pela mensagem completa | telegram_id=%s",
                candidate.message_id,
                exc_info=True,
            )
            return await client.download_media(
                candidate.fallback_message,
                file=str(job_dir),
            )

    async def _download_open_graph_image(
        self,
        text: str,
        job_dir: Path,
    ) -> Path | None:
        """Fallback to the public page's og:image when Telegram has no ready preview."""

        urls = [match.group(0).rstrip(".,);]}") for match in URL_RE.finditer(text)]
        if not urls:
            return None

        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for page_url in urls[:3]:
                try:
                    async with session.get(page_url, allow_redirects=True) as response:
                        if response.status >= 400:
                            continue
                        content_type = response.headers.get("Content-Type", "").lower()
                        if "text/html" not in content_type:
                            continue
                        body = await response.text(errors="ignore")
                        base_url = str(response.url)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue

                image_url = self._extract_open_graph_image(body, base_url)
                if not image_url:
                    continue

                try:
                    async with session.get(image_url, allow_redirects=True) as image_response:
                        if image_response.status >= 400:
                            continue
                        image_type = image_response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                        if not image_type.startswith("image/"):
                            continue
                        data = await image_response.read()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue

                if not data or len(data) > MAX_FALLBACK_IMAGE_BYTES:
                    continue

                suffix = mimetypes.guess_extension(image_type) or Path(
                    urlparse(image_url).path
                ).suffix
                if suffix.lower() == ".jpe":
                    suffix = ".jpg"
                if not suffix or len(suffix) > 6:
                    suffix = ".jpg"
                path = job_dir / f"open_graph{suffix.lower()}"
                path.write_bytes(data)
                return path.resolve()

        return None

    @staticmethod
    def _extract_open_graph_image(document: str, base_url: str) -> str | None:
        for pattern in (OG_IMAGE_RE, OG_IMAGE_RE_REVERSED):
            match = pattern.search(document)
            if match:
                return urljoin(base_url, html.unescape(match.group(1).strip()))
        return None

    @staticmethod
    def _validate_downloaded_path(downloaded: str | bytes | None) -> Path | None:
        if downloaded is None or isinstance(downloaded, bytes):
            return None
        path = Path(downloaded).resolve()
        if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            return None
        return path

    @classmethod
    def _messages_indicate_media(cls, messages: list[Any]) -> bool:
        for message in messages:
            if any(
                getattr(message, attribute, None) is not None
                for attribute in ("photo", "video", "document")
            ):
                return True
            preview = getattr(message, "web_preview", None)
            if preview is not None and any(
                getattr(preview, attribute, None) is not None
                for attribute in ("photo", "document")
            ):
                return True
        return False

    @staticmethod
    def _preview_may_be_pending(messages: list[Any]) -> bool:
        if not messages:
            return True
        for message in messages:
            preview = getattr(message, "web_preview", None)
            if preview is None:
                media = getattr(message, "media", None)
                if media is not None and "webpage" in type(media).__name__.lower():
                    return True
                continue
            type_name = type(preview).__name__.lower()
            if "pending" in type_name or "empty" in type_name:
                return True
            if (
                getattr(preview, "photo", None) is None
                and getattr(preview, "document", None) is None
            ):
                return True
        return False

    @staticmethod
    def _mime_type(media: Any) -> str:
        return str(getattr(media, "mime_type", "") or "").lower()

    def cleanup(self, job: PublishJob) -> None:
        """Remove all temporary media belonging to a job."""

        shutil.rmtree(self._job_dir(job), ignore_errors=True)

    def _job_dir(self, job: PublishJob) -> Path:
        return self.media_root / safe_path_component(job.job_id)

    @staticmethod
    def _remove_empty_dir(directory: Path) -> None:
        try:
            directory.rmdir()
        except OSError:
            pass
