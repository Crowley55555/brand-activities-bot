"""HTTP фазы 5 (ADR-39, ADR-40, ADR-42): колесо, Polaroid и подарок за пост. Все маршруты под HMAC.

POST /internal/v1/activities/wheel/spin - спин колеса (T15): 200 {prize_title, reward_id}.
POST /internal/v1/activities/photo_frame/accept - приём фото (декларация MIME и размера).
POST /internal/v1/activities/confirmation/accept - ссылка на пост. Решения модератора - только в
/admin/*
Гость находится по channel_type + external_id (как booking). Все запросы под HMAC.
Ошибки ядра уходят в JSON {"error": code} через register_error_handlers.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.adapters.random import SystemRandomPort
from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.common.errors import NotFound, ValidationError
from app.core.activities.confirmation import accept_confirmation
from app.core.activities.photo_frame import accept_source
from app.core.activities.wheel import spin
from app.core.channels import lookup_user
from app.core.ports.clock import ClockPort
from app.db.unit_of_work import UnitOfWork

router = APIRouter(prefix="/internal/v1/activities")
ChannelTypeIn = Literal["telegram", "max", "whatsapp", "web"]


class GuestActivityIn(BaseModel):
    channel_type: ChannelTypeIn
    external_id: str
    activity_slug: str


class WheelSpinOut(BaseModel):
    prize_title: str
    reward_id: int


class PhotoAcceptIn(GuestActivityIn):
    tg_file_id: str
    mime_type: str
    file_size_bytes: int


class ConfirmationAcceptIn(GuestActivityIn):
    post_url: str


class ActivityAcceptOut(BaseModel):
    submission_id: int
    attempt_id: int
    status: str


def _parse[T: BaseModel](model: type[T], body: bytes) -> T:
    try:
        return model.model_validate_json(body)
    except PydanticValidationError as exc:
        raise ValidationError("bad_request") from exc


@router.post("/wheel/spin", response_model=WheelSpinOut)
async def wheel_spin(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> WheelSpinOut:
    payload = _parse(GuestActivityIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    result = await spin(
        r.consents,
        r.catalog,
        r.attempts,
        r.jobs,
        r.rewards,
        r.wheel,
        SystemRandomPort(),
        channel.user_id,
        payload.activity_slug,
        channel.id,
        clock.now(),
    )
    await uow.commit()
    return WheelSpinOut(prize_title=result.prize_title, reward_id=result.reward_id)


@router.post("/photo_frame/accept", response_model=ActivityAcceptOut)
async def photo_frame_accept(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> ActivityAcceptOut:
    payload = _parse(PhotoAcceptIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    accepted = await accept_source(
        r.consents,
        r.catalog,
        r.attempts,
        r.frames,
        r.jobs,
        channel.user_id,
        payload.activity_slug,
        channel.id,
        payload.tg_file_id,
        payload.mime_type,
        payload.file_size_bytes,
        clock.now(),
    )
    await uow.commit()
    return ActivityAcceptOut(
        submission_id=accepted.submission_id,
        attempt_id=accepted.attempt_id,
        status=accepted.status,
    )


@router.post("/confirmation/accept", response_model=ActivityAcceptOut)
async def confirmation_accept(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> ActivityAcceptOut:
    payload = _parse(ConfirmationAcceptIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    accepted = await accept_confirmation(
        r.consents,
        r.catalog,
        r.attempts,
        r.ugc,
        channel.user_id,
        payload.activity_slug,
        channel.id,
        payload.post_url,
        clock.now(),
    )
    await uow.commit()
    return ActivityAcceptOut(
        submission_id=int(accepted.submission_id),
        attempt_id=int(accepted.attempt_id),
        status=accepted.status,
    )
