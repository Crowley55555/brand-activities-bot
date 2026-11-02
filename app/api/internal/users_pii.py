from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.api.schemas import PiiUpdateIn, PiiUpdateOut
from app.common.errors import NotFound
from app.core.channels import lookup_user
from app.core.consent import CURRENT_POLICY_VERSION
from app.core.ports.clock import ClockPort
from app.core.users import update_pii
from app.core.users.pii_normalize import (
    normalize_email,
    normalize_name,
    normalize_phone,
)
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.users_pii")

router = APIRouter(prefix="/internal/v1/users")


@router.post("/pii/update", response_model=PiiUpdateOut)
async def update_pii_endpoint(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> PiiUpdateOut:
    payload = PiiUpdateIn.model_validate_json(body)

    # Правило 5: допустимые версии политики проверяет core.consent;
    # здесь — только тот факт, что клиент прислал корректное имя.
    if payload.policy_version != CURRENT_POLICY_VERSION:
        from app.common.errors import ValidationError

        raise ValidationError("policy_version_unsupported")

    channel = await lookup_user(
        uow.repositories.channels, payload.channel_type, payload.external_id
    )
    if channel is None:
        raise NotFound("channel_not_found")

    first_name = normalize_name(payload.first_name)
    last_name = normalize_name(payload.last_name)
    phone_e164 = normalize_phone(payload.phone)
    email_lower = normalize_email(payload.email)

    user_ref = await update_pii(
        users=uow.repositories.users,
        consents=uow.repositories.consents,
        user_id=channel.user_id,
        first_name=first_name,
        last_name=last_name,
        phone_e164=phone_e164,
        email_lower=email_lower,
        now=clock.now(),
    )

    logger.info(
        "pii_updated",
        extra={
            "event": "pii_updated",
            "user_id": int(user_ref.id),
            "channel_type": payload.channel_type,
        },
    )
    await uow.commit()  # F2: ответ уходит только после фиксации ПДн
    return PiiUpdateOut(status="ok", user_id=int(user_ref.id))
