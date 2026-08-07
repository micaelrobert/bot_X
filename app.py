"""Application entry point and service supervisor."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from config import ConfigurationError, Settings
from logger import setup_logging
from media import MediaManager
from models import PublishResult
from queue_manager import QueueManager
from rate_limiter import PublishRateLimiter
from storage import PendingStore, PublishedStore, RateLimitStore
from telegram_listener import TelegramListener
from x_publisher import AmbiguousPublishError, PlaywrightXPublisher, XPublisher

LOGGER = logging.getLogger(__name__)


async def publisher_worker(
    queue: QueueManager,
    listener: TelegramListener,
    publisher: XPublisher,
    media: MediaManager,
    rate_limiter: PublishRateLimiter,
) -> None:
    """Consume the durable queue forever and publish each job sequentially."""

    while True:
        job = await queue.get()
        x_accepted = False
        needs_reconciliation = job.state == "publishing"
        try:
            if await queue.published.contains_any(job.source_keys):
                await queue.pending.remove(job.job_id)
                continue

            if needs_reconciliation:
                reconciled = await publisher.reconcile(job)
                if reconciled is not None:
                    await queue.complete(job, reconciled)
                    media.cleanup(job)
                    continue
                await queue.mark_queued(job)
                needs_reconciliation = False

            media_paths = await media.download_for_job(
                listener.client,
                listener.channel_entity,
                job,
            )
            if not job.text and not media_paths:
                LOGGER.warning(
                    "Mensagem sem texto e sem mídia suportada; marcada como processada | job=%s",
                    job.job_id,
                )
                job.last_attempt_at = job.created_at
                await queue.complete(job, PublishResult(x_id=None, x_url=None))
                continue

            await rate_limiter.wait_and_reserve()
            
            # Verificar se o tweet já existe no X antes de publicar (idempotência)
            if job.state == "publishing":
                reconciled = await publisher.reconcile(job)
                if reconciled is not None:
                    LOGGER.info(
                        "Tweet já existe no X; marcando como concluído | job=%s | x_id=%s",
                        job.job_id,
                        reconciled.x_id,
                    )
                    await queue.complete(job, reconciled)
                    media.cleanup(job)
                    continue
            
            await queue.mark_publishing(job)
            result = await publisher.publish(job, media_paths)
            x_accepted = True
            await queue.complete(job, result)
            media.cleanup(job)
        except asyncio.CancelledError:
            raise
        except AmbiguousPublishError as exc:
            media.cleanup(job)
            delay = await queue.fail(job, exc, ambiguous=True)
            LOGGER.exception(
                "Resultado de publicação ambíguo; haverá reconciliação | job=%s | retry=%ss",
                job.job_id,
                delay,
            )
        except Exception as exc:
            media.cleanup(job)
            delay = await queue.fail(
                job,
                exc,
                ambiguous=x_accepted or needs_reconciliation,
            )
            LOGGER.exception(
                "Falha no processamento; job permanecerá na fila | job=%s | retry=%ss | ambiguous=%s",
                job.job_id,
                delay,
                x_accepted or needs_reconciliation,
            )
            try:
                await publisher.reset()
            except Exception:
                LOGGER.exception("Falha ao reiniciar o navegador do X")
        finally:
            queue.task_done()


async def run_service(settings: Settings, stop_event: asyncio.Event) -> None:
    """Run one supervised service lifecycle."""

    published = PublishedStore(settings.published_path)
    pending = PendingStore(settings.pending_path)
    queue = QueueManager(
        pending=pending,
        published=published,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
    )
    listener = TelegramListener(settings, queue)
    publisher = PlaywrightXPublisher(settings)
    media = MediaManager(settings.media_dir, settings.x_max_images)
    rate_limiter = PublishRateLimiter(
        RateLimitStore(settings.rate_limit_path),
        settings.publish_interval_seconds,
    )

    worker_task: asyncio.Task[None] | None = None
    listener_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None

    try:
        if settings.recover_pending_on_start:
            await queue.recover()
        else:
            await queue.discard_pending()

        await listener.start()
        await publisher.start()

        worker_task = asyncio.create_task(
            publisher_worker(queue, listener, publisher, media, rate_limiter),
            name="x-publisher-worker",
        )
        listener_task = asyncio.create_task(
            listener.wait_until_disconnected(),
            name="telegram-listener",
        )
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")

        LOGGER.info(
            "Serviço ativo e aguardando novas mensagens | intervalo=%ss | recuperar_pendentes=%s",
            settings.publish_interval_seconds,
            settings.recover_pending_on_start,
        )
        done, _ = await asyncio.wait(
            {worker_task, listener_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task in done and not stop_event.is_set():
            exception = worker_task.exception()
            raise RuntimeError(
                "O worker de publicação foi encerrado inesperadamente"
            ) from exception
        if listener_task in done and not stop_event.is_set():
            exception = listener_task.exception()
            raise ConnectionError(
                "Telethon foi desconectado inesperadamente"
            ) from exception
    finally:
        for task in (worker_task, listener_task, stop_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [
            task for task in (worker_task, listener_task, stop_task) if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await queue.close()
        await publisher.stop()
        await listener.stop()


async def watch_stop_request(path: Path, stop_event: asyncio.Event) -> None:
    """Translate a control-panel stop file into a graceful shutdown signal."""

    while not stop_event.is_set():
        if path.exists():
            try:
                path.unlink()
            except OSError:
                LOGGER.warning("Não foi possível remover o pedido de parada", exc_info=True)
            LOGGER.info("Pedido de parada recebido pelo painel de controle")
            stop_event.set()
            return
        await asyncio.sleep(0.5)


async def supervisor() -> None:
    """Restart the service after failures until a signal or panel stop request."""

    settings = Settings.load()
    setup_logging(settings.logs_dir, settings.log_level)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    stop_request_path = settings.storage_dir / "stop.request"
    pid_path = settings.storage_dir / "bot.pid"

    stop_request_path.unlink(missing_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def request_shutdown() -> None:
        LOGGER.info("Sinal de encerramento recebido")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_shutdown())

    control_task = asyncio.create_task(
        watch_stop_request(stop_request_path, stop_event),
        name="control-panel-stop-watcher",
    )

    try:
        while not stop_event.is_set():
            try:
                await run_service(settings, stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Falha crítica do ciclo; reiniciando em %s segundos",
                    settings.retry_base_seconds,
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.retry_base_seconds,
                    )
                except TimeoutError:
                    pass

        LOGGER.info("Serviço encerrado com segurança")
    finally:
        if not control_task.done():
            control_task.cancel()
        await asyncio.gather(control_task, return_exceptions=True)
        stop_request_path.unlink(missing_ok=True)
        try:
            current_pid = pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            current_pid = ""
        if current_pid == str(os.getpid()):
            pid_path.unlink(missing_ok=True)


def main() -> None:
    """Synchronous CLI entry point."""

    try:
        asyncio.run(supervisor())
    except ConfigurationError as exc:
        raise SystemExit(f"Erro de configuração: {exc}") from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
