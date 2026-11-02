"""Outbox telegram_send: /internal/v1/channels/telegram/outbox/{claim,ack} (ADR-32).

Claim отдаёт готовый текст: рендер делает ядро (п.8 ревью), tg_worker видит только chat_id и text.
Ack принимает исход отправки и worker_id: fencing (ТОП-3) не даёт чужому воркеру перезаписать
исход. Оба маршрута под HMAC (правило 10), как остальные /internal/v1/*.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.config import Settings, get_settings
from app.core.jobs import OutboxTexts, ack_outbox, claim_outbox
from app.core.media import frame_preview_url
from app.core.ports.clock import ClockPort
from app.core.types.ids import JobId
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.telegram_outbox")
router = APIRouter(prefix="/internal/v1/channels/telegram/outbox")


class OutboxClaimIn(BaseModel):
    worker_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=10, ge=1, le=50)
    visibility_timeout_s: int = Field(default=90, ge=10, le=900)


class OutboxMessageOut(BaseModel):
    chat_id: int
    text: str
    # M-1 (фаза 5): абсолютный URL фото; при наличии tg_worker шлёт фото (send_photo).
    media_url: str | None = None


class OutboxJobOut(BaseModel):
    id: int
    payload: OutboxMessageOut


class OutboxClaimOut(BaseModel):
    jobs: list[OutboxJobOut]


class OutboxAckIn(BaseModel):
    job_id: int = Field(ge=1)
    worker_id: str = Field(min_length=1, max_length=64)
    result: Literal["success", "retryable", "terminal_business", "dead"]
    error: str | None = Field(default=None, max_length=200)
    retry_after_s: int | None = Field(default=None, ge=0, le=86400)


class OutboxAckOut(BaseModel):
    status: Literal["ack"] = "ack"


def outbox_texts(settings: Settings) -> OutboxTexts:
    """Тексты уведомлений из конфига (правило 5)."""
    return OutboxTexts(
        reward_notice=settings.outbox_text_reward_notice,
        ugc_rejected_retry=settings.outbox_text_ugc_rejected_retry,
        ugc_rejected_closed=settings.outbox_text_ugc_rejected_closed,
        ingest_failed_retry=settings.outbox_text_ingest_failed_retry,
        ingest_failed_closed=settings.outbox_text_ingest_failed_closed,
        appointment_reminder=settings.outbox_text_appointment_reminder,
        appointment_auto_cancelled=settings.outbox_text_appointment_auto_cancelled,
        appointment_leaflet=settings.outbox_text_appointment_leaflet,
        public_base_url=settings.public_base_url,
        wheel_result=settings.outbox_text_wheel_result,
        polaroid_ready=settings.outbox_text_polaroid_ready,
        promocode=settings.outbox_text_promocode,
        confirmation_rejected=settings.outbox_text_confirmation_rejected,
    )


# media_url (M-1) попадает в ответ только у фото: у текстовых сообщений контракт chat_id+text
# прежний.
@router.post("/claim", response_model=OutboxClaimOut, response_model_exclude_none=True)
async def claim(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OutboxClaimOut:
    payload = OutboxClaimIn.model_validate_json(body)
    repos = uow.repositories
    messages = await claim_outbox(
        repos.jobs,
        repos.rewards,
        repos.catalog,
        outbox_texts(settings),
        worker_id=payload.worker_id,
        limit=payload.limit,
        visibility_timeout_s=payload.visibility_timeout_s,
        appointments=repos.appointments,
        frame_link=lambda frame_id: frame_preview_url(
            settings.internal_api_auth_secret, frame_id
        ),
    )
    await uow.commit()  # F2: блокировки и терминалы рендера фиксируются до ответа
    return OutboxClaimOut(
        jobs=[
            OutboxJobOut(
                id=int(message.job_id),
                payload=OutboxMessageOut(
                    chat_id=message.chat_id, text=message.text, media_url=message.media_url
                ),
            )
            for message in messages
        ]
    )


@router.post("/ack", response_model=OutboxAckOut)
async def ack(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> OutboxAckOut:
    payload = OutboxAckIn.model_validate_json(body)
    repos = uow.repositories
    await ack_outbox(
        repos.jobs,
        repos.channels,
        JobId(payload.job_id),
        payload.worker_id,
        payload.result,
        payload.error,
        payload.retry_after_s,
        clock.now(),
    )
    await uow.commit()
    logger.info(
        "outbox_acked",
        extra={"event": "outbox_acked", "job_id": payload.job_id, "result": payload.result},
    )
    return OutboxAckOut()
