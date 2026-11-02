"""/internal/v1/channels/tg/fsm: персистентный FSM-storage для tg_worker (C-4).

Правило 4 (внешний сервис через порт): tg_worker ходит сюда HTTP под HMAC, не ходит в
основную БД сам. Правило 10 (auth): все методы под `verify_internal_hmac`. На выходе
только state + data; технические поля (updated_at) наружу не отдаём.

Канон контракта:
* POST `/get`     — тело `{"key": "..."}` (HMAC считает по этому телу).
  Ответ 200 `{state, data}` или `{state: null, data: {}}` если записи нет.
* POST `/set`     — тело `{"key": "...", "state": <str|null>, "data": {...}, "user_id"?: int}`.
  Полная замена state + data. Остался для совместимости (storage после 10b-3
  зовёт `/set_state_only` и `/set_data_only` вместо него).
* POST `/set_state_only` — тело `{"key": "...", "state": <str|null>, "user_id"?: int}`.
  Атомарный UPSERT только `state`, data не трогается (N-2 фазы 10b-3).
* POST `/set_data_only`  — тело `{"key": "...", "data": {...}, "user_id"?: int}`.
  Атомарный UPSERT только `data`, state не трогается (N-2 фазы 10b-3).
* POST `/clear`   — тело `{"key": "..."}`. Ответ 200 `{}` (повтор idempotent).

HTTP-метод DELETE технически без тела тоже валиден, но у нас HMAC считается по телу,
а некоторые клиенты не умеют DELETE с телом стабильно. Поэтому используем POST с явными
action: `/get`, `/set`, `/set_state_only`, `/set_data_only`, `/clear`.

`user_id` — опциональный BIGINT из `users.id` в основной БД: tg_worker после регистрации
знает `user_id` гостя и прокидывает его сюда, чтобы `/forget` (фаза 10b-3, I-2) мог
удалить все FSM-строки пользователя за один DELETE WHERE user_id (миграция 0021).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.core.channels.tg_fsm import (
    clear_state,
    get_state,
    set_data_only,
    set_state,
    set_state_only,
)
from app.core.ports.clock import ClockPort
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.channels_tg_fsm")
router = APIRouter(prefix="/internal/v1/channels/tg/fsm")

_KEY_MAX_LENGTH = 512


class _KeyBody(BaseModel):
    key: str = Field(min_length=1, max_length=_KEY_MAX_LENGTH)


class FsmSetIn(BaseModel):
    key: str = Field(min_length=1, max_length=_KEY_MAX_LENGTH)
    state: str | None = Field(default=None, max_length=512)
    data: dict[str, Any] = Field(default_factory=dict)
    user_id: int | None = None


class FsmSetStateOnlyIn(BaseModel):
    key: str = Field(min_length=1, max_length=_KEY_MAX_LENGTH)
    state: str | None = Field(default=None, max_length=512)
    user_id: int | None = None


class FsmSetDataOnlyIn(BaseModel):
    key: str = Field(min_length=1, max_length=_KEY_MAX_LENGTH)
    data: dict[str, Any] = Field(default_factory=dict)
    user_id: int | None = None


class FsmStateOut(BaseModel):
    state: str | None = None
    data: Mapping[str, Any] = Field(default_factory=dict)


class FsmAckOut(BaseModel):
    status: str = "ok"


@router.post("/get", response_model=FsmStateOut)
async def fsm_get(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> FsmStateOut:
    payload = _KeyBody.model_validate_json(body)
    row = await get_state(uow.repositories.tg_fsm, payload.key)
    # Чтение не требует commit (SELECT), uow.__aexit__ сам закроет сессию.
    if row is None:
        return FsmStateOut(state=None, data={})
    return FsmStateOut(state=row.state, data=dict(row.data))


@router.post("/set", response_model=FsmAckOut)
async def fsm_set(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> FsmAckOut:
    payload = FsmSetIn.model_validate_json(body)
    await set_state(
        uow.repositories.tg_fsm,
        payload.key,
        payload.state,
        payload.data,
        clock.now(),
        user_id=payload.user_id,
    )
    await uow.commit()  # F2: клиент получает 200 только после фиксации записи
    return FsmAckOut()


@router.post("/set_state_only", response_model=FsmAckOut)
async def fsm_set_state_only(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> FsmAckOut:
    """N-2 (фаза 10b-3): атомарный UPSERT state без read-modify-write."""
    payload = FsmSetStateOnlyIn.model_validate_json(body)
    await set_state_only(
        uow.repositories.tg_fsm,
        payload.key,
        payload.state,
        clock.now(),
        user_id=payload.user_id,
    )
    await uow.commit()
    return FsmAckOut()


@router.post("/set_data_only", response_model=FsmAckOut)
async def fsm_set_data_only(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> FsmAckOut:
    """N-2 (фаза 10b-3): атомарный UPSERT data без read-modify-write."""
    payload = FsmSetDataOnlyIn.model_validate_json(body)
    await set_data_only(
        uow.repositories.tg_fsm,
        payload.key,
        payload.data,
        clock.now(),
        user_id=payload.user_id,
    )
    await uow.commit()
    return FsmAckOut()


@router.post("/clear", response_model=FsmAckOut)
async def fsm_clear(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> FsmAckOut:
    payload = _KeyBody.model_validate_json(body)
    await clear_state(uow.repositories.tg_fsm, payload.key)
    await uow.commit()
    return FsmAckOut()
