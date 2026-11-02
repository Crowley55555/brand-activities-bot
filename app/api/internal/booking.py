"""HTTP ядра для booking (ADR-36): list_slots, book, cancel. Все под HMAC (X-Internal-Auth).

Ошибки ядра уходят в JSON {"error": code} через register_error_handlers:
409 slot_taken / already_passed_today / consent_required / not_cancellable,
403 attempt_not_owned, 404 appointment_not_found / channel_not_found, 422 bad_request.
Схемы живут здесь, потому что относятся только к этому контракту (08 §4.3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.common.errors import NotFound, ValidationError
from app.core.activities.booking import book_slot, cancel_by_user, list_free_slots_today
from app.core.catalog import get_activity_by_slug
from app.core.channels import lookup_user
from app.core.ports.clock import ClockPort
from app.core.types.ids import AppointmentId, SlotId
from app.db.unit_of_work import UnitOfWork

router = APIRouter(prefix="/internal/v1/activities/booking")

ChannelTypeIn = Literal["telegram", "max", "whatsapp", "web"]


class BookingListSlotsIn(BaseModel):
    channel_type: ChannelTypeIn
    external_id: str
    activity_slug: str


class BookingSlotOut(BaseModel):
    slot_id: int
    start_at: datetime
    duration_minutes: int


class BookingListSlotsOut(BaseModel):
    slots: list[BookingSlotOut]


class BookingBookIn(BaseModel):
    channel_type: ChannelTypeIn
    external_id: str
    activity_slug: str
    slot_id: int


class BookingBookOut(BaseModel):
    appointment_id: int
    slot_start: datetime
    status: str
    remind_before_minutes: int
    has_reminders: bool


class BookingCancelIn(BaseModel):
    channel_type: ChannelTypeIn
    external_id: str
    appointment_id: int


class BookingCancelOut(BaseModel):
    status: str


def _parse[ModelT: BaseModel](model: type[ModelT], body: bytes) -> ModelT:
    """Тело запроса -> модель; кривой JSON/поля дают 422 bad_request, а не 500."""
    try:
        return model.model_validate_json(body)
    except PydanticValidationError as exc:
        raise ValidationError("bad_request") from exc


@router.post("/list_slots", response_model=BookingListSlotsOut)
async def list_slots(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> BookingListSlotsOut:
    payload = _parse(BookingListSlotsIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    activity = await get_activity_by_slug(r.catalog, payload.activity_slug)
    if activity.kind != "booking":
        raise ValidationError("activity_kind_mismatch")
    slots = await list_free_slots_today(r.slots, activity.id, clock.now())
    return BookingListSlotsOut(
        slots=[
            BookingSlotOut(
                slot_id=int(s.id), start_at=s.start_at, duration_minutes=s.duration_minutes
            )
            for s in slots
        ]
    )


@router.post("/book", response_model=BookingBookOut)
async def book(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> BookingBookOut:
    payload = _parse(BookingBookIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    result = await book_slot(
        r,
        channel.user_id,
        payload.activity_slug,
        channel.id,
        SlotId(payload.slot_id),
        clock.now(),
    )
    await uow.commit()  # F2: клиент получает 2xx только после фиксации записи
    return BookingBookOut(
        appointment_id=int(result.appointment_id),
        slot_start=result.slot_start,
        status=result.status,
        remind_before_minutes=result.remind_before_minutes,
        has_reminders=result.has_reminders,
    )


@router.post("/cancel", response_model=BookingCancelOut)
async def cancel(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> BookingCancelOut:
    payload = _parse(BookingCancelIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    result = await cancel_by_user(
        r, AppointmentId(payload.appointment_id), channel.user_id, clock.now()
    )
    await uow.commit()
    return BookingCancelOut(status=result.status)
