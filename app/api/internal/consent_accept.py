from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.api.schemas import ConsentAcceptIn, ConsentAcceptOut
from app.common.errors import NotFound
from app.core.channels import lookup_user
from app.core.consent import CURRENT_POLICY_VERSION, accept
from app.core.ports.clock import ClockPort
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.consent_accept")

router = APIRouter(prefix="/internal/v1/consent")


@router.post("/accept", response_model=ConsentAcceptOut)
async def accept_consent(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> ConsentAcceptOut:
    payload = ConsentAcceptIn.model_validate_json(body)
    channel = await lookup_user(
        uow.repositories.channels, payload.channel_type, payload.external_id
    )
    if channel is None:
        raise NotFound("channel_not_found")

    await accept(
        repo=uow.repositories.consents,
        user_id=channel.user_id,
        channel_type=payload.channel_type,
        now=clock.now(),
    )
    logger.info(
        "consent_accepted",
        extra={
            "event": "consent_accepted",
            "user_id": int(channel.user_id),
            "channel_type": payload.channel_type,
            "policy_version": CURRENT_POLICY_VERSION,
        },
    )
    await uow.commit()  # F2: ответ уходит только после фиксации согласия
    return ConsentAcceptOut(
        status="ok",
        user_id=int(channel.user_id),
        policy_version=CURRENT_POLICY_VERSION,
    )
