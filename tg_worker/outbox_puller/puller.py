"""Outbox puller tg_worker (ADR-32): claim telegram_send -> send_message -> ack исхода.

Исходы (канон 04 §3.3, ADR-32):
- успех -> success;
- Retry-After (flood) -> retryable с retry_after_s точно;
- 403 (пользователь заблокировал бота) -> terminal_business user_blocked (канал помечается);
- 400 (некорректные данные, чат не найден) -> terminal_business bad_request;
- сеть, 5xx -> retryable; прочие ошибки Telegram -> retryable.
Fencing (ТОП-3): каждый ack несёт тот же worker_id, что и claim. Чужой или снятый замок даёт 409
на стороне ядра, и такой ack просто логируется.
Сбой внутри одной задачи (пункт E) не роняет пачку: задача уходит в retryable, остальные
доставляются. В last_error уходит только имя класса исключения, без текста сообщения: токены и
ПДн в очередь не попадают.
Доставка - at-least-once: если ack не дошёл, задача вернётся после visibility timeout и уйдёт
повторно. Гарантии на стороне Telegram нет (канон 04 §4, строка 22).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Mapping
from typing import Any, Final, Protocol

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

logger = logging.getLogger("tg_worker.outbox_puller")
POLL_INTERVAL_S: Final[float] = 1.0
CLAIM_LIMIT: Final[int] = 10
VISIBILITY_TIMEOUT_S: Final[int] = 90


def default_worker_id() -> str:
    """Идентификатор по умолчанию: hostname:pid (OUTBOX_WORKER_ID переопределяет)."""
    return f"{socket.gethostname()}:{os.getpid()}"


class MessageSender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...

    async def send_photo(self, chat_id: int, photo: str, caption: str, **kwargs: Any) -> Any: ...


# Telegram не смог скачать фото по нашей ссылке (сетевой сбой на нашей стороне): повторим позже.
_PHOTO_FETCH_FAILED: Final[str] = "failed to get HTTP URL content"


class OutboxApi(Protocol):
    async def outbox_claim(
        self, worker_id: str, limit: int, visibility_timeout_s: int
    ) -> list[dict[str, Any]]: ...

    async def outbox_ack(
        self,
        job_id: int,
        worker_id: str,
        result: str,
        error: str | None = None,
        retry_after_s: int | None = None,
    ) -> None: ...


async def _ack(
    api: OutboxApi,
    job_id: int,
    worker_id: str,
    result: str,
    error: str | None = None,
    retry_after_s: int | None = None,
) -> None:
    try:
        await api.outbox_ack(
            job_id=job_id,
            worker_id=worker_id,
            result=result,
            error=error,
            retry_after_s=retry_after_s,
        )
    except Exception as exc:
        # Ack не дошёл (в т.ч. 409 fencing): задача вернётся после visibility timeout.
        logger.error(
            "outbox_ack_failed",
            extra={"event": "outbox_ack_failed", "job_id": job_id, "error": type(exc).__name__},
        )


async def _deliver(
    bot: MessageSender,
    api: OutboxApi,
    worker_id: str,
    item: Mapping[str, Any],
) -> None:
    job_id = int(item["id"])
    payload = item["payload"]
    media_url = payload.get("media_url")
    try:
        if media_url:
            # M-1: фото по ссылке на наш /media/frame; Telegram скачивает файл сам, текст - подпись.
            await bot.send_photo(
                chat_id=int(payload["chat_id"]),
                photo=str(media_url),
                caption=str(payload["text"]),
            )
        else:
            await bot.send_message(chat_id=int(payload["chat_id"]), text=str(payload["text"]))
    except TelegramRetryAfter as exc:
        await _ack(api, job_id, worker_id, "retryable", "telegram_flood", int(exc.retry_after))
    except TelegramForbiddenError:
        await _ack(api, job_id, worker_id, "terminal_business", "user_blocked")
    except TelegramBadRequest as exc:
        if media_url and _PHOTO_FETCH_FAILED in str(exc):
            # Текст ошибки в last_error не пишем: только наш слаг (ПДн и токены не попадают).
            await _ack(api, job_id, worker_id, "retryable", "photo_fetch_failed")
        else:
            await _ack(api, job_id, worker_id, "terminal_business", "bad_request")
    except (TelegramNetworkError, TelegramServerError):
        await _ack(api, job_id, worker_id, "retryable", "telegram_unavailable")
    except TelegramAPIError:
        await _ack(api, job_id, worker_id, "retryable", "telegram_api_error")
    else:
        await _ack(api, job_id, worker_id, "success")


async def pull_once(
    bot: MessageSender,
    api: OutboxApi,
    worker_id: str,
    limit: int = CLAIM_LIMIT,
    visibility_timeout_s: int = VISIBILITY_TIMEOUT_S,
) -> int:
    """Одна пачка: claim, отправка каждого сообщения, ack исхода. Возвращает число задач."""
    items = await api.outbox_claim(worker_id, limit, visibility_timeout_s)
    for item in items:
        job_id = int(item["id"])
        try:
            await _deliver(bot, api, worker_id, item)
        except Exception as exc:
            logger.error(
                "delivery_batch_item_failed",
                extra={
                    "event": "delivery_batch_item_failed",
                    "job_id": job_id,
                    "error": type(exc).__name__,
                },
            )
            await _ack(api, job_id, worker_id, "retryable", type(exc).__name__)
    return len(items)


async def run_outbox_puller(
    bot: MessageSender,
    api: OutboxApi,
    worker_id: str,
    stop: asyncio.Event,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> None:
    """Цикл до остановки. Ошибка ядра не роняет процесс: следующая попытка через интервал."""
    while not stop.is_set():
        try:
            processed = await pull_once(bot, api, worker_id)
        except Exception as exc:
            logger.error(
                "outbox_pull_failed",
                extra={"event": "outbox_pull_failed", "error": type(exc).__name__},
            )
            processed = 0
        if processed == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)
            except TimeoutError:
                continue
