"""Экран /admin/rewards (фаза 7b) + /admin/rewards/redeem* (фаза 10b-1, C-1).

Тонкий слой (правило 3): разбор query/формы, вызов core.rewards, рендер.
Гость в списке — только внутренним user_id (ПДн на экране нет, правило 9).

Экран погашения (фаза 10b-1):
* `GET /admin/rewards/redeem` — пустая форма ввода OTP + CSRF.
* `POST /admin/rewards/redeem` — найти награду по OTP: показать карточку гостя
  (маскированные имя/фамилия + telegram_username) + кнопку «погасил». На любой
  промах формируется та же форма с ошибкой «код не найден или истёк».
* `POST /admin/rewards/redeem/{reward_id}` — атомарное погашение, 302 на redeem.

CSRF проверяет app-level `csrf_guard`. Auth — AdminSessionMiddleware +
`current_admin`. Правило 10: оба слоя на месте.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import RedirectResponse, Response

from app.admin.templating import render_page
from app.api.deps import current_admin, get_clock, get_uow
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.common.errors import ConflictError
from app.config import Settings, get_settings
from app.core.ports.clock import ClockPort
from app.core.rewards import OtpCard, find_by_otp, list_rewards_page, redeem_with_otp
from app.core.types.dto import AdminRef, RewardListRow
from app.core.types.ids import AdminId, RewardId
from app.db.unit_of_work import UnitOfWork

router = APIRouter()
logger = logging.getLogger("app.admin.rewards")

PER_PAGE: Final[int] = 50  # размер страницы - не бизнес-правило, константа слоя
_TIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M"

REDEEM_PATH: Final[str] = "/admin/rewards/redeem"
# Канон 02 §7: OTP ровно 6 цифр; длиннее/короче ввод — та же ошибка «код не найден»,
# чтобы админ не заметил, что мы угадали форму.
_OTP_LENGTH: Final[int] = 6


@dataclass(frozen=True)
class RewardItem:
    id: int
    issued_label: str
    reward_type: str
    code: str
    title: str
    activity_slug: str
    activity_display_name: str
    user_id: int
    status: str
    status_label: str


def _item(row: RewardListRow) -> RewardItem:
    if row.revoked_at is not None:
        status, label = "revoked", f"отозван {row.revoked_at.strftime(_TIME_FORMAT)}"
    elif row.redeemed_at is not None:
        status, label = "redeemed", f"погашен {row.redeemed_at.strftime(_TIME_FORMAT)}"
    else:
        status, label = "issued", "выдан"
    return RewardItem(
        id=row.id,
        issued_label=row.issued_at.strftime(_TIME_FORMAT),
        reward_type=row.reward_type,
        code=row.code or "—",
        title=row.title or "—",
        activity_slug=row.activity_slug,
        activity_display_name=row.activity_display_name,
        user_id=row.user_id,
        status=status,
        status_label=label,
    )


@router.get("/rewards")
async def rewards_list(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
    type: Annotated[str, Query(pattern="^(all|promocode|voucher)$")] = "promocode",  # noqa: A002
    status: Annotated[str, Query(pattern="^(all|issued|redeemed|revoked)$")] = "issued",
    activity_slug: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
) -> Response:
    slug = activity_slug or None
    rows, total = await list_rewards_page(
        uow.repositories.rewards, type, status, slug, PER_PAGE, (page - 1) * PER_PAGE
    )
    params = {"type": type, "status": status}
    if slug:
        params["activity_slug"] = slug
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(
        "rewards_list.html",
        title="Награды",
        admin=admin,
        csrf=csrf_value,
        items=[_item(row) for row in rows],
        total=total,
        page=page,
        has_prev=page > 1,
        has_next=page * PER_PAGE < total,
        type=type,
        status=status,
        activity_slug=slug or "",
        base_query=urlencode(params),
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


# ----- /admin/rewards/redeem (фаза 10b-1, C-1) --------------------------------


def _redeem_form(
    request: Request,
    settings: Settings,
    admin: AdminRef,
    *,
    error: str | None = None,
    otp_value: str = "",
    card: OtpCard | None = None,
) -> Response:
    """Страница формы погашения. Карточка (если есть) рисуется под формой."""
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(
        "rewards_redeem.html",
        title="Погасить награду",
        admin=admin,
        csrf=csrf_value,
        error=error,
        otp_value=otp_value,
        card=card,
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


@router.get("/rewards/redeem")
async def redeem_form(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Пустая форма ввода OTP."""
    return _redeem_form(request, settings, admin)


@router.post("/rewards/redeem")
async def redeem_lookup(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    otp: Annotated[str, Form()] = "",
) -> Response:
    """Поиск награды по OTP → карточка владельца или ошибка «код не найден или истёк».

    HTTP 200 с ошибкой (а не 404), чтобы админ остался на той же форме и мог ввести
    другой OTP, не теряя контекст (ревью промта §4, тест 19).
    """
    raw = otp.strip()
    # Короткий фильтр по форме: не ищем в БД, если ввод явно не 6 цифр.
    # Админ видит общее «код не найден или истёк» — то же сообщение, что для истёкших,
    # чтобы форма ввода не подсказывала допустимую длину посторонним глазам.
    if len(raw) != _OTP_LENGTH or not raw.isdigit():
        logger.info(
            "redeem_lookup_bad_format",
            extra={"event": "redeem_lookup_bad_format", "admin_id": admin.id},
        )
        return _redeem_form(
            request, settings, admin, error="otp_not_found", otp_value=raw
        )
    try:
        card = await find_by_otp(uow.repositories.rewards, raw, clock.now())
    except ConflictError as exc:
        logger.info(
            "redeem_lookup_rejected",
            extra={
                "event": "redeem_lookup_rejected",
                "admin_id": admin.id,
                "reason": exc.code,
            },
        )
        return _redeem_form(request, settings, admin, error=exc.code, otp_value=raw)
    if card is None:
        logger.info(
            "redeem_lookup_not_found",
            extra={"event": "redeem_lookup_not_found", "admin_id": admin.id},
        )
        return _redeem_form(request, settings, admin, error="otp_not_found", otp_value=raw)
    logger.info(
        "redeem_lookup_ok",
        extra={
            "event": "redeem_lookup_ok",
            "admin_id": admin.id,
            "reward_id": int(card.reward_id),
            "reward_type": card.reward_type,
        },
    )
    return _redeem_form(request, settings, admin, otp_value=raw, card=card)


@router.post("/rewards/redeem/{reward_id}")
async def redeem_action(
    reward_id: int,
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    otp: Annotated[str, Form()] = "",
) -> Response:
    """Погасить награду. На промахе — та же форма с ошибкой."""
    raw = otp.strip()
    if len(raw) != _OTP_LENGTH or not raw.isdigit():
        return _redeem_form(
            request, settings, admin, error="otp_not_found", otp_value=raw
        )
    try:
        # fix-1 I-2 (10b-1): передаём `reward_id` из URL в ядро, ядро гасит
        # ровно эту награду или бросает `ConflictError('otp_mismatch')`. Это
        # закрывает UX-риск «набрал OTP чужого гостя под открытой карточкой A»:
        # ядро не согласится погасить B под карточкой A, админ видит понятную
        # ошибку, никакой награды не трогаем.
        redeemed_id = await redeem_with_otp(
            uow.repositories.rewards,
            raw,
            AdminId(admin.id),
            clock.now(),
            reward_id=RewardId(reward_id),
        )
    except ConflictError as exc:
        logger.info(
            "redeem_action_rejected",
            extra={
                "event": "redeem_action_rejected",
                "admin_id": admin.id,
                "reward_id": reward_id,
                "reason": exc.code,
            },
        )
        # Без commit: ConflictError (включая otp_mismatch) означает «не гасили
        # ничего», транзакция откатится при закрытии UoW.
        return _redeem_form(request, settings, admin, error=exc.code, otp_value=raw)
    await uow.commit()  # F2: 302 уходит после фиксации погашения.
    logger.info(
        "redeem_action_ok",
        extra={
            "event": "redeem_action_ok",
            "admin_id": admin.id,
            "reward_id": int(redeemed_id),
        },
    )
    return RedirectResponse(REDEEM_PATH, status_code=302)
