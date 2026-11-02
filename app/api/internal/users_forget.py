"""/internal/v1/users/forget — `/forget` каскад (фаза 10b-3, I-2).

Канон 08 §335 + правило 10: HMAC-подпись + body. tg_worker вызывает после двойного
подтверждения гостем «ПОДТВЕРЖДАЮ». Ядро анонимизирует ПДн, hard delete UGC и
tg_fsm_state, ставит s3_delete джобы.

ПДн в payload нет — только `channel_type` и `external_id` канала. В ответе —
счётчики удалённых строк (для логирования tg_worker).
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.common.errors import NotFound, ValidationError
from app.core.channels import lookup_user
from app.core.personal_data import confirm_forget, request_forget
from app.core.ports.clock import ClockPort
from app.core.types.ids import UserId
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.users_forget")

router = APIRouter(prefix="/internal/v1/users")


class ForgetIn(BaseModel):
    channel_type: Literal["telegram", "max", "whatsapp", "web"]
    external_id: str


class ForgetOut(BaseModel):
    status: Literal["ok"]
    user_id: int
    ugc_submissions_deleted: int
    tg_fsm_rows_deleted: int
    s3_delete_jobs_enqueued: int


@router.post("/forget", response_model=ForgetOut)
async def forget_user_endpoint(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> ForgetOut:
    """POST /internal/v1/users/forget — анонимизация + каскад UGC/FSM (I-2)."""
    try:
        payload = ForgetIn.model_validate_json(body)
    except PydanticValidationError as exc:
        raise ValidationError("bad_request") from exc

    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")

    # request_forget — guard: NotFound/Conflict уйдут как 404/409 через error_handler.
    await request_forget(r.users, UserId(channel.user_id))
    result = await confirm_forget(
        r.users,
        r.consents,
        r.ugc,
        r.tg_fsm,
        r.jobs,
        UserId(channel.user_id),
        clock.now(),
    )
    # F2: 200 только после фиксации анонимизации и s3_delete джобов.
    await uow.commit()
    logger.info(
        "user_forgotten",
        extra={
            "event": "user_forgotten",
            "user_id": result.user_id,
            "ugc_deleted": result.ugc_submissions_deleted,
            "fsm_deleted": result.tg_fsm_rows_deleted,
            "s3_delete_enqueued": result.s3_delete_jobs_enqueued,
        },
    )
    return ForgetOut(
        status="ok",
        user_id=result.user_id,
        ugc_submissions_deleted=result.ugc_submissions_deleted,
        tg_fsm_rows_deleted=result.tg_fsm_rows_deleted,
        s3_delete_jobs_enqueued=result.s3_delete_jobs_enqueued,
    )
