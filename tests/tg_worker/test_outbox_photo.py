"""Puller tg_worker с media_url (M-1, фаза 5): фото по ссылке через send_photo, сбой загрузки -
повтор.

Фейки повторяют контракт ядра (outbox/claim, outbox/ack) и бота aiogram; сети и Telegram нет.
"""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from tg_worker.outbox_puller.puller import pull_once

WORKER = "w-photo"
PHOTO_URL = "https://app.test/media/frame/77?t=signed"
PHOTO_JOB = {
    "id": 9,
    "payload": {"chat_id": 555, "text": "Ваш Polaroid готов!", "media_url": PHOTO_URL},
}
TEXT_JOB = {"id": 10, "payload": {"chat_id": 555, "text": "Привет"}}


class _Api:
    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        self._jobs = jobs
        self.acks: list[dict[str, Any]] = []

    async def outbox_claim(
        self, worker_id: str, limit: int, visibility_timeout_s: int
    ) -> list[dict[str, Any]]:
        return self._jobs

    async def outbox_ack(
        self,
        job_id: int,
        worker_id: str,
        result: str,
        error: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        self.acks.append({"job_id": job_id, "result": result, "error": error})


class _Bot:
    """send_photo/send_message записывают вызовы; send_photo может бросить заданную ошибку."""

    def __init__(self, photo_error: Exception | None = None) -> None:
        self._photo_error = photo_error
        self.photos: list[dict[str, Any]] = []
        self.texts: list[dict[str, Any]] = []

    async def send_photo(self, chat_id: int, photo: str, caption: str, **kwargs: Any) -> None:
        if self._photo_error is not None:
            raise self._photo_error
        self.photos.append({"chat_id": chat_id, "photo": photo, "caption": caption})

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.texts.append({"chat_id": chat_id, "text": text})


async def test_media_url_is_sent_as_photo_with_caption() -> None:
    bot = _Bot()
    api = _Api([PHOTO_JOB])

    await pull_once(bot, api, WORKER)  # type: ignore[arg-type]

    assert bot.photos == [{"chat_id": 555, "photo": PHOTO_URL, "caption": "Ваш Polaroid готов!"}]
    assert bot.texts == []
    assert api.acks[0]["result"] == "success"


async def test_job_without_media_url_still_uses_send_message() -> None:
    bot = _Bot()
    api = _Api([TEXT_JOB])

    await pull_once(bot, api, WORKER)  # type: ignore[arg-type]

    assert bot.texts == [{"chat_id": 555, "text": "Привет"}]
    assert bot.photos == []
    assert api.acks[0]["result"] == "success"


async def test_photo_fetch_failure_is_retryable() -> None:
    """Telegram не смог скачать фото по нашей ссылке: повтор с слагом, текст ошибки не пишем."""
    exc = TelegramBadRequest(
        method=SendMessage(chat_id=555, text="x"),
        message="Bad Request: failed to get HTTP URL content",
    )
    bot = _Bot(photo_error=exc)
    api = _Api([PHOTO_JOB])

    await pull_once(bot, api, WORKER)  # type: ignore[arg-type]

    assert api.acks[0]["result"] == "retryable"
    assert api.acks[0]["error"] == "photo_fetch_failed"
