"""Decoupled X publisher implemented with Playwright browser automation."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config import Settings
from models import PublishJob, PublishResult
from text_utils import URL_RE, normalize_text
from utils import ensure_absolute_paths

LOGGER = logging.getLogger(__name__)
CREATE_POST_RE = re.compile(r"CreateTweet|CreatePost", re.IGNORECASE)
STATUS_ID_RE = re.compile(r"/status/(\d+)")


class PublisherError(RuntimeError):
    """Base class for publisher failures."""


class AuthenticationRequiredError(PublisherError):
    """Raised when the persistent browser profile is not authenticated."""


class AmbiguousPublishError(PublisherError):
    """Raised after the Post click when success could not be confirmed."""


class XPublisher(ABC):
    """Port that allows browser automation to be replaced by the official API."""

    @abstractmethod
    async def start(self) -> None:
        """Initialize publisher resources."""

    @abstractmethod
    async def publish(self, job: PublishJob, media_paths: list[Path]) -> PublishResult:
        """Publish one job."""

    @abstractmethod
    async def reconcile(self, job: PublishJob) -> PublishResult | None:
        """Try to find a post created during an ambiguous previous attempt."""

    @abstractmethod
    async def reset(self) -> None:
        """Recreate transient resources after a browser failure."""

    @abstractmethod
    async def stop(self) -> None:
        """Release publisher resources."""


class PlaywrightXPublisher(XPublisher):
    """Publish posts through a dedicated persistent Chromium profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start Chromium with a dedicated persistent user-data directory."""

        if self._context is not None:
            return
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.x_profile_dir),
            channel="msedge",
            headless=True,
            locale="pt-BR",
            viewport={"width": 1440, "height": 1000},
            args=[
                "--disable-dev-shm-usage",
                "--headless=new",
                "--disable-gpu",
                "--window-position=-32000,-32000",
                "--window-size=1,1",
            ],
        )
        self._context.set_default_timeout(self.settings.x_navigation_timeout_ms)
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        LOGGER.info("Navegador do X iniciado | headless=True (forçado)")

    async def stop(self) -> None:
        """Close browser resources without deleting the persistent login state."""

        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        self._page = None

        if context is not None:
            try:
                await context.close()
            except Exception:
                LOGGER.warning("Falha ao fechar o contexto do Chromium", exc_info=True)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                LOGGER.warning("Falha ao finalizar o Playwright", exc_info=True)

    async def reset(self) -> None:
        """Restart the browser while preserving the profile directory."""

        try:
            await self.stop()
        finally:
            await self.start()

    async def publish(self, job: PublishJob, media_paths: list[Path]) -> PublishResult:
        """Compose and publish a post, confirming media before clicking Post."""

        async with self._lock:
            await self._ensure_started()
            page = await self._open_composer()
            composer = page.locator('[data-testid="tweetTextarea_0"]').first

            if job.text:
                await composer.click()
                await composer.fill(job.text)

            normalized_media = [Path(path).resolve() for path in media_paths]
            if normalized_media:
                await self._attach_media(page, job, normalized_media)

            post_button = await self._find_visible_post_button(page)
            await self._wait_until_post_ready(
                page=page,
                post_button=post_button,
                media_required=bool(normalized_media),
            )

            if normalized_media and not await self._media_is_attached(page):
                await self._capture_upload_failure(page, job, "preview_desapareceu")
                raise PublisherError(
                    "A mídia desapareceu do compositor antes da publicação; "
                    "o texto não será enviado sozinho"
                )

            LOGGER.info("Publicando no X... | job=%s", job.job_id)

            clicked = False
            try:
                async with page.expect_response(
                    lambda response: (
                        bool(CREATE_POST_RE.search(response.url))
                        and response.request.method == "POST"
                    ),
                    timeout=self.settings.x_upload_timeout_ms,
                ) as response_info:
                    clicked = True
                    await post_button.click()
                response = await response_info.value
            except PlaywrightTimeoutError as exc:
                if clicked:
                    raise AmbiguousPublishError(
                        "O botão Publicar foi acionado, mas a resposta do X não pôde ser confirmada"
                    ) from exc
                raise PublisherError("Tempo esgotado antes de publicar no X") from exc
            except Exception as exc:
                if clicked:
                    raise AmbiguousPublishError(
                        "Falha após o acionamento do botão Publicar"
                    ) from exc
                raise

            try:
                payload = await response.json()
            except Exception as exc:
                if response.ok:
                    raise AmbiguousPublishError(
                        "O X respondeu, mas o resultado da publicação não pôde ser interpretado"
                    ) from exc
                raise PublisherError(f"Falha HTTP do X: {response.status}") from exc

            if not isinstance(payload, dict):
                raise AmbiguousPublishError(
                    "O X respondeu com um formato inesperado após a publicação"
                )

            if not response.ok or payload.get("errors"):
                raise PublisherError(
                    f"O X rejeitou a publicação: HTTP {response.status}; "
                    f"erros={payload.get('errors')}"
                )

            x_id = self._extract_created_post_id(payload)
            x_url = self._build_status_url(x_id)
            LOGGER.info(
                "Publicado com sucesso | telegram_ids=%s | x_id=%s | job=%s | midias=%s",
                job.message_ids,
                x_id or "não informado",
                job.job_id,
                len(normalized_media),
            )
            return PublishResult(x_id=x_id, x_url=x_url)

    async def _attach_media(
        self,
        page: Page,
        job: PublishJob,
        media_paths: list[Path],
    ) -> None:
        """Select local files and wait until X visibly attaches them."""

        missing = [str(path) for path in media_paths if not path.is_file()]
        if missing:
            raise PublisherError(f"Arquivos de mídia não encontrados: {missing}")

        LOGGER.info(
            "Preparando mídia para o X | job=%s | arquivos=%s",
            job.job_id,
            [f"{path.name}:{path.stat().st_size}" for path in media_paths],
        )

        file_input = page.locator('input[data-testid="fileInput"][type="file"]').first
        try:
            await file_input.wait_for(
                state="attached",
                timeout=self.settings.x_navigation_timeout_ms,
            )
            await file_input.set_input_files(ensure_absolute_paths(media_paths))
        except Exception as exc:
            await self._capture_upload_failure(page, job, "falha_selecao_arquivo")
            raise PublisherError("Não foi possível selecionar a mídia no X") from exc

        try:
            input_count = await file_input.evaluate(
                "element => element.files ? element.files.length : 0"
            )
        except Exception:
            input_count = -1
        LOGGER.info(
            "Arquivos selecionados no input do X | job=%s | quantidade=%s",
            job.job_id,
            input_count,
        )

        try:
            await self._wait_for_media_attachment(page)
        except Exception:
            await self._capture_upload_failure(page, job, "anexo_nao_confirmado")
            raise

        LOGGER.info(
            "Anexo confirmado no compositor do X | job=%s | arquivos=%s",
            job.job_id,
            len(media_paths),
        )

    async def _wait_for_media_attachment(self, page: Page) -> None:
        """Wait for a visible X attachment preview and for upload progress to end."""

        deadline = time.monotonic() + self.settings.x_upload_timeout_ms / 1000
        stable_since: float | None = None

        while time.monotonic() < deadline:
            attached = await self._media_is_attached(page)
            busy = await self._media_upload_is_busy(page)

            if attached and not busy:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 2.0:
                    return
            else:
                stable_since = None

            await asyncio.sleep(0.4)

        raise PublisherError(
            "O X não confirmou o anexo da mídia. A publicação foi cancelada "
            "para evitar enviar somente o texto"
        )

    async def _media_is_attached(self, page: Page) -> bool:
        """Return whether the composer currently contains a visible media preview."""

        selectors = (
            '[data-testid="attachments"]',
            '[data-testid="mediaPreview"]',
            '[data-testid="tweetPhoto"]',
            '[data-testid="videoPlayer"]',
            '[data-testid="removeMedia"]',
            'button[aria-label*="Remove media" i]',
            'button[aria-label*="Remover mídia" i]',
            'div[aria-label*="Media" i] img',
        )
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 8)):
                try:
                    if await locator.nth(index).is_visible():
                        return True
                except Exception:
                    continue
        return False

    async def _media_upload_is_busy(self, page: Page) -> bool:
        """Detect progress indicators associated with the composer attachment."""

        selectors = (
            '[data-testid="attachments"] [role="progressbar"]',
            '[data-testid="mediaPreview"] [role="progressbar"]',
            '[data-testid="attachments"] progress',
            '[data-testid="mediaPreview"] progress',
            '[aria-label*="Uploading" i]',
            '[aria-label*="Carregando" i]',
            '[aria-label*="Processando" i]',
        )
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 8)):
                try:
                    if await locator.nth(index).is_visible():
                        return True
                except Exception:
                    continue
        return False

    async def _capture_upload_failure(
        self,
        page: Page,
        job: PublishJob,
        reason: str,
    ) -> None:
        """Save a screenshot that helps diagnose future X interface changes."""

        safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", job.job_id)
        screenshot = self.settings.logs_dir / f"x_media_{reason}_{safe_job}.png"
        try:
            await page.screenshot(path=str(screenshot), full_page=False)
            LOGGER.error(
                "Captura de diagnóstico salva | job=%s | arquivo=%s",
                job.job_id,
                screenshot,
            )
        except Exception:
            LOGGER.warning(
                "Não foi possível salvar captura de diagnóstico | job=%s",
                job.job_id,
                exc_info=True,
            )

    async def reconcile(self, job: PublishJob) -> PublishResult | None:
        """Find an exact recent text match after an ambiguous click/restart boundary."""

        if (
            not self.settings.twitter_username
            or not job.text
            or not job.last_attempt_at
        ):
            return None

        signature = self._text_signature(job.text)
        if len(signature) < 12:
            return None

        async with self._lock:
            await self._ensure_started()
            page = self._require_page()
            username = self.settings.twitter_username.lstrip("@").strip()
            await page.goto(
                f"{self.settings.x_base_url}/{username}",
                wait_until="domcontentloaded",
            )
            await self._ensure_logged_in(page)

            try:
                attempted_at = datetime.fromisoformat(job.last_attempt_at)
            except ValueError:
                attempted_at = datetime.now(UTC)
            if attempted_at.tzinfo is None:
                attempted_at = attempted_at.replace(tzinfo=UTC)

            articles = page.locator('article[data-testid="tweet"]')
            try:
                await articles.first.wait_for(state="visible", timeout=15_000)
            except PlaywrightTimeoutError:
                return None

            # Buscar em mais tweets e com janela de tempo maior para aumentar chance de reconciliação
            max_tweets_to_check = 50
            time_window = timedelta(hours=2)
            
            for index in range(min(await articles.count(), max_tweets_to_check)):
                article = articles.nth(index)
                text_locator = article.locator('[data-testid="tweetText"]')
                if await text_locator.count() == 0:
                    continue
                visible_text = await text_locator.inner_text()
                visible_signature = self._text_signature(visible_text)
                
                # Comparação mais flexível para lidar com pequenas diferenças
                match_found = (
                    visible_signature == signature
                    or visible_signature.startswith(f"{signature} ")
                    or signature.startswith(f"{visible_signature} ")
                )
                if not match_found:
                    continue

                time_locator = article.locator("time").first
                timestamp = await time_locator.get_attribute("datetime")
                if timestamp:
                    try:
                        post_time = datetime.fromisoformat(
                            timestamp.replace("Z", "+00:00")
                        )
                        if abs(post_time - attempted_at) > time_window:
                            continue
                    except ValueError:
                        pass

                links = article.locator('a[href*="/status/"]')
                for link_index in range(await links.count()):
                    href = await links.nth(link_index).get_attribute("href")
                    match = STATUS_ID_RE.search(href or "")
                    if match:
                        x_id = match.group(1)
                        LOGGER.warning(
                            "Publicação reconciliada após resultado ambíguo | job=%s | x_id=%s",
                            job.job_id,
                            x_id,
                        )
                        x_url = (
                            href
                            if (href or "").startswith("http")
                            else f"{self.settings.x_base_url}{href}"
                        )
                        return PublishResult(
                            x_id=x_id,
                            x_url=x_url,
                            reconciled=True,
                        )
        return None

    async def _ensure_started(self) -> None:
        if self._context is None or self._page is None or self._page.is_closed():
            await self.start()

    async def _open_composer(self) -> Page:
        page = self._require_page()
        await page.goto(
            f"{self.settings.x_base_url}/compose/post",
            wait_until="domcontentloaded",
        )
        if await self._composer_is_visible(page):
            return page

        await self._ensure_logged_in(page)
        await page.goto(
            f"{self.settings.x_base_url}/compose/post",
            wait_until="domcontentloaded",
        )
        try:
            await page.locator('[data-testid="tweetTextarea_0"]').first.wait_for(
                state="visible",
                timeout=self.settings.x_navigation_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise PublisherError(
                "O compositor do X não foi localizado. A interface pode ter mudado."
            ) from exc
        return page

    async def _ensure_logged_in(self, page: Page) -> None:
        if await self._authenticated_marker_is_visible(page):
            return
        if not self.settings.x_auto_login:
            raise AuthenticationRequiredError(
                "Sessão do X ausente. Execute: python scripts/x_login.py"
            )
        await self._automated_login(page)

    async def _automated_login(self, page: Page) -> None:
        """Best-effort credential login; manual persistent login remains preferred."""

        assert self.settings.twitter_username is not None
        assert self.settings.twitter_password is not None

        await page.goto(
            f"{self.settings.x_base_url}/i/flow/login",
            wait_until="domcontentloaded",
        )
        username_input = page.locator('input[autocomplete="username"]').first
        await username_input.wait_for(state="visible")
        await username_input.fill(self.settings.twitter_username)
        await self._click_named_button(page, r"Next|Avançar|Seguinte")

        challenge_input = page.locator(
            'input[data-testid="ocfEnterTextTextInput"]'
        ).first
        try:
            await challenge_input.wait_for(state="visible", timeout=4_000)
            challenge_value = (
                self.settings.twitter_email or self.settings.twitter_username
            )
            await challenge_input.fill(challenge_value)
            await self._click_named_button(page, r"Next|Avançar|Seguinte")
        except PlaywrightTimeoutError:
            pass

        password_input = page.locator('input[name="password"]').first
        await password_input.wait_for(state="visible")
        await password_input.fill(self.settings.twitter_password)

        login_button = page.locator('[data-testid="LoginForm_Login_Button"]').first
        if await login_button.count() and await login_button.is_visible():
            await login_button.click()
        else:
            await self._click_named_button(page, r"Log in|Entrar")

        try:
            authenticated_marker = page.locator(
                '[data-testid="SideNav_NewTweet_Button"], '
                '[data-testid="AppTabBar_Home_Link"]'
            ).first
            await authenticated_marker.wait_for(
                state="visible",
                timeout=self.settings.x_navigation_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise AuthenticationRequiredError(
                "Login automático não concluído; pode haver 2FA, CAPTCHA ou desafio de segurança. "
                "Use python scripts/x_login.py."
            ) from exc

    async def _click_named_button(self, page: Page, pattern: str) -> None:
        button = page.get_by_role(
            "button", name=re.compile(pattern, re.IGNORECASE)
        ).first
        await button.wait_for(state="visible")
        await button.click()

    async def _composer_is_visible(self, page: Page) -> bool:
        composer = page.locator('[data-testid="tweetTextarea_0"]').first
        try:
            await composer.wait_for(state="visible", timeout=5_000)
            return True
        except PlaywrightTimeoutError:
            return False

    async def _authenticated_marker_is_visible(self, page: Page) -> bool:
        marker = page.locator(
            '[data-testid="tweetTextarea_0"], '
            '[data-testid="SideNav_NewTweet_Button"], '
            '[data-testid="AppTabBar_Home_Link"], '
            '[data-testid="SideNav_AccountSwitcher_Button"]'
        ).first
        try:
            await marker.wait_for(state="visible", timeout=8_000)
            return True
        except PlaywrightTimeoutError:
            return False

    async def _find_visible_post_button(self, page: Page) -> Locator:
        candidates = page.locator(
            '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
        )
        deadline = time.monotonic() + self.settings.x_navigation_timeout_ms / 1000
        while time.monotonic() < deadline:
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible():
                    return candidate
            await asyncio.sleep(0.25)
        raise PublisherError("Botão Publicar não encontrado no compositor do X")

    async def _wait_until_post_ready(
        self,
        page: Page,
        post_button: Locator,
        media_required: bool,
    ) -> None:
        """Wait for an enabled post button without losing a required attachment."""

        deadline = time.monotonic() + self.settings.x_upload_timeout_ms / 1000
        while time.monotonic() < deadline:
            media_ok = not media_required or await self._media_is_attached(page)
            upload_busy = media_required and await self._media_upload_is_busy(page)
            if media_ok and not upload_busy and await post_button.is_enabled():
                return
            await asyncio.sleep(0.5)
        raise PublisherError(
            "A mídia não terminou de carregar ou o post não ficou habilitado"
        )

    def _require_page(self) -> Page:
        if self._page is None:
            raise PublisherError("Navegador não inicializado")
        return self._page

    def _build_status_url(self, x_id: str | None) -> str | None:
        if not x_id or not self.settings.twitter_username:
            return None
        username = self.settings.twitter_username.lstrip("@").strip()
        return f"{self.settings.x_base_url}/{username}/status/{x_id}"

    @staticmethod
    def _extract_created_post_id(payload: dict[str, Any]) -> str | None:
        try:
            result = payload["data"]["create_tweet"]["tweet_results"]["result"]
        except (KeyError, TypeError):
            try:
                result = payload["data"]["create_post"]["post_results"]["result"]
            except (KeyError, TypeError):
                return None

        def find_rest_id(value: Any) -> str | None:
            if isinstance(value, dict):
                rest_id = value.get("rest_id")
                if rest_id is not None and str(rest_id).isdigit():
                    return str(rest_id)
                for child in value.values():
                    found = find_rest_id(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = find_rest_id(child)
                    if found:
                        return found
            return None

        return find_rest_id(result)

    @staticmethod
    def _text_signature(text: str) -> str:
        without_urls = URL_RE.sub(" ", normalize_text(text)).casefold()
        return re.sub(r"\s+", " ", without_urls).strip()
