from __future__ import annotations

import logging
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.api.schemas import UpdateIn, UpdateOut
from app.core.channels import ensure_account
from app.core.ports.clock import ClockPort
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.telegram")

router = APIRouter(prefix="/internal/v1/channels/telegram")

MSK = ZoneInfo("Europe/Moscow")


def _parse_start(update: dict[str, Any]) -> tuple[str, str, str | None] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if text != "/start":
        return None
    from_ = message.get("from")
    chat = message.get("chat")
    if not isinstance(from_, dict) or not isinstance(chat, dict):
        return None
    tg_user_id = from_.get("id")
    chat_id = chat.get("id")
    if tg_user_id is None or chat_id is None:
        return None
    username = from_.get("username")
    return str(tg_user_id), str(chat_id), (str(username) if username else None)


@router.post("/updates", response_model=UpdateOut)
async def receive_telegram_update(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> UpdateOut:
    payload = UpdateIn.model_validate_json(body)
    parsed = _parse_start(payload.update)
    if parsed is None:
        logger.info(
            "update_ignored",
            extra={"event": "update_ignored", "update_type": "non_start"},
        )
        return UpdateOut(status="accepted", reply=None)

    external_id, chat_id, username = parsed
    now = clock.now()
    user, created = await ensure_account(
        uow.repositories.users,
        uow.repositories.channels,
        "telegram",
        external_id,
        username,
        now,
    )

    # Приветственный reply формирует tg_worker (знает про меню/consent-FSM);
    # ядро только сообщает факт регистрации, чтобы адаптер выбрал ветку.
    logger.info(
        "start_handled",
        extra={
            "event": "start_handled",
            "update_type": "start",
            "user_id": user.id,
            "channel_account_created": created,
            "is_registered": user.is_registered,
        },
    )
    await uow.commit()  # F2: ответ боту уходит только после фиксации гостя
    return UpdateOut(
        status="accepted",
        reply=None,
        is_registered=user.is_registered,
    )
