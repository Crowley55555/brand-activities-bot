"""HTTP /internal/v1/rewards/* под HMAC (фаза 10b-1, C-1).

Два эндпоинта для tg_worker:

* `POST /list_active` — активные именные награды гостя (voucher, physical_gift).
  Берём user_id по channel_type + external_id (как остальные /internal/v1/activities/*).
  Ответ: `{"rewards": [{reward_id, reward_type, title, issued_at}]}`.
  Код ваучера не выдаём: сам смысл фазы 10b-1 — гость идёт за OTP, а не за кодом.
  Если нет действующей привязки — 404 `channel_not_found`.
* `POST /{reward_id}/request_otp` — выдать OTP на именную награду этого гостя.
  Канон 04 §1.2: гость в боте жмёт «подойду сейчас», ядро пишет OTP + expires_at.
  Ответ: `{"otp": "123456", "expires_at": "...", "ttl_minutes": 5}`.
  Ошибки ядра (`reward_not_eligible`, `reward_not_found`, `already_redeemed`,
  `reward_revoked`) уходят через ConflictError → 409.

ПДн в ответе нет: `OtpOut.otp` — одноразовый технический код, не ПДн.
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
from app.config import Settings, get_settings
from app.core.channels import lookup_user
from app.core.ports.clock import ClockPort
from app.core.rewards import request_otp
from app.core.types.ids import RewardId, UserId
from app.db.unit_of_work import UnitOfWork

router = APIRouter(prefix="/internal/v1/rewards")

ChannelTypeIn = Literal["telegram", "max", "whatsapp", "web"]


class _ListActiveIn(BaseModel):
    channel_type: ChannelTypeIn
    external_id: str


class _RequestOtpIn(BaseModel):
    channel_type: ChannelTypeIn
    external_id: str


class ActiveRewardOut(BaseModel):
    reward_id: int
    reward_type: str
    title: str | None
    issued_at: datetime


class ListActiveOut(BaseModel):
    rewards: list[ActiveRewardOut]


class RequestOtpOut(BaseModel):
    otp: str
    expires_at: datetime
    ttl_minutes: int


def _parse[T: BaseModel](model: type[T], body: bytes) -> T:
    try:
        return model.model_validate_json(body)
    except PydanticValidationError as exc:
        raise ValidationError("bad_request") from exc


@router.post("/list_active", response_model=ListActiveOut)
async def list_active(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> ListActiveOut:
    """Активные именные награды гостя (voucher, physical_gift) для `/my` в tg_worker."""
    payload = _parse(_ListActiveIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    rows = await r.rewards.list_active_named_rewards_by_user(UserId(channel.user_id))
    return ListActiveOut(
        rewards=[
            ActiveRewardOut(
                reward_id=int(row.reward_id),
                reward_type=row.reward_type,
                title=row.title,
                issued_at=row.issued_at,
            )
            for row in rows
        ]
    )


@router.post("/{reward_id}/request_otp", response_model=RequestOtpOut)
async def request_otp_endpoint(
    reward_id: int,
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RequestOtpOut:
    """Выдать OTP на именную награду гостя (канон 04 §1.2)."""
    payload = _parse(_RequestOtpIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    ttl = settings.rewards_otp_ttl_minutes
    issued = await request_otp(
        r.rewards,
        RewardId(reward_id),
        UserId(channel.user_id),
        clock.now(),
        ttl,
    )
    # F2: клиент получает 200 только после фиксации OTP.
    await uow.commit()
    return RequestOtpOut(otp=issued.otp, expires_at=issued.expires_at, ttl_minutes=ttl)
