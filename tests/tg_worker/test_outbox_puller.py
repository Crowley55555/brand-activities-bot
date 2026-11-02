"""Outbox puller tg_worker (ADR-32): исходы отправки -> ack с worker_id (fencing, ТОП-3).

Пункт E ревью: сбой одной задачи не роняет пачку, в ack уходит только имя класса исключения.
"""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from tg_worker.outbox_puller.puller import pull_once

WORKER = "tg-test:1"
JOB: dict[str, Any] = {"id": 42, "payload": {"chat_id": 555, "text": "Привет"}}
JOB_2: dict[str, Any] = {"id": 43, "payload": {"chat_id": 556, "text": "Второе"}}


class FakeApi:
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
        self.acks.append(
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "result": result,
                "error": error,
                "retry_after_s": retry_after_s,
            }
        )


class FakeBot:
    """Каждый send_message берёт следующий эффект: исключение или None (успех)."""

    def __init__(self, effects: list[Exception | None] | None = None) -> None:
        self._effects = list(effects or [])
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        effect = self._effects.pop(0) if self._effects else None
        if effect is not None:
            raise effect
        self.sent.append((chat_id, text))


def _method() -> SendMessage:
    return SendMessage(chat_id=555, text="Привет")


async def test_success_acks_success() -> None:
    bot, api = FakeBot(), FakeApi([JOB])
    processed = await pull_once(bot, api, WORKER)  # type: ignore[arg-type]
    assert processed == 1
    assert bot.sent == [(555, "Привет")]
    assert api.acks == [
        {
            "job_id": 42,
            "worker_id": WORKER,
            "result": "success",
            "error": None,
            "retry_after_s": None,
        }
    ]


async def test_forbidden_is_terminal_user_blocked() -> None:
    exc = TelegramForbiddenError(method=_method(), message="Forbidden: bot was blocked")
    api = FakeApi([JOB])
    await pull_once(FakeBot([exc]), api, WORKER)  # type: ignore[arg-type]
    assert api.acks[0]["result"] == "terminal_business"
    assert api.acks[0]["error"] == "user_blocked"
    assert api.acks[0]["worker_id"] == WORKER


async def test_retry_after_is_retryable_with_seconds() -> None:
    exc = TelegramRetryAfter(method=_method(), message="Too Many Requests", retry_after=7)
    api = FakeApi([JOB])
    await pull_once(FakeBot([exc]), api, WORKER)  # type: ignore[arg-type]
    assert api.acks[0]["result"] == "retryable"
    assert api.acks[0]["retry_after_s"] == 7


async def test_bad_request_is_terminal() -> None:
    exc = TelegramBadRequest(method=_method(), message="Bad Request: chat not found")
    api = FakeApi([JOB])
    await pull_once(FakeBot([exc]), api, WORKER)  # type: ignore[arg-type]
    assert api.acks[0]["result"] == "terminal_business"
    assert api.acks[0]["error"] == "bad_request"


async def test_network_error_is_retryable() -> None:
    exc = TelegramNetworkError(method=_method(), message="timeout")
    api = FakeApi([JOB])
    await pull_once(FakeBot([exc]), api, WORKER)  # type: ignore[arg-type]
    assert api.acks[0]["result"] == "retryable"
    assert api.acks[0]["error"] == "telegram_unavailable"


async def test_unexpected_error_in_one_item_does_not_stop_batch() -> None:
    """Пункт E: RuntimeError на первом сообщении -> retryable с именем класса; второе доставлено."""
    bot = FakeBot([RuntimeError("boom with secret-looking details")])
    api = FakeApi([JOB, JOB_2])

    processed = await pull_once(bot, api, WORKER)  # type: ignore[arg-type]

    assert processed == 2
    assert bot.sent == [(556, "Второе")]
    assert api.acks[0] == {
        "job_id": 42,
        "worker_id": WORKER,
        "result": "retryable",
        "error": "RuntimeError",
        "retry_after_s": None,
    }
    assert api.acks[1]["result"] == "success"
    assert api.acks[1]["job_id"] == 43
