"""Экран /admin/users (фаза 7a): список зарегистрированных, ПДн замаскированы.

Тонкий слой (правило 3): параметры страницы, вызов core.users, рендер. Показ телефона - только
POST с CSRF (ADR-22): ядро пишет запись в audit_log и отдаёт номер, страница рисуется в этом же
ответе. Номер не попадает ни в URL, ни в журнал приложения (правила 2 и 9), а прямого GET на
показ нет - поэтому каждый показ номера оставляет след в аудите.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import Response

from app.admin.templating import render_page
from app.api.deps import current_admin, get_clock, get_uow
from app.api.middleware.csrf import attach_csrf_cookie, current_csrf_value
from app.common.errors import ConflictError
from app.common.pii_display import mask_email, mask_name, mask_phone
from app.config import Settings, get_settings
from app.core.ports.clock import ClockPort
from app.core.types.dto import AdminRef, UserListRow
from app.core.types.ids import AdminId, UserId
from app.core.users import list_users_page, reveal_user_phone
from app.db.unit_of_work import UnitOfWork

router = APIRouter()
logger = logging.getLogger("app.admin.users")

PAGE_SIZE: Final[int] = 50  # размер страницы - не бизнес-правило, константа слоя
_TIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M"


@dataclass(frozen=True)
class UserItem:
    """Строка списка для шаблона: ПДн уже замаскированы (или раскрыт один телефон)."""

    user_id: int
    created_label: str
    registered_label: str
    last_seen_label: str
    is_anonymized: bool
    first_name: str
    last_name: str
    phone: str
    phone_revealed: bool
    email: str


def _label(value: object) -> str:
    return value.strftime(_TIME_FORMAT) if hasattr(value, "strftime") else "—"


def _item(row: UserListRow, revealed: tuple[int, str] | None) -> UserItem:
    uid = int(row.id)
    show = revealed is not None and revealed[0] == uid and not row.is_anonymized
    return UserItem(
        user_id=uid,
        created_label=_label(row.created_at),
        registered_label=_label(row.registered_at),
        last_seen_label=_label(row.last_seen_at),
        is_anonymized=row.is_anonymized,
        first_name=mask_name(row.first_name),
        last_name=mask_name(row.last_name),
        phone=revealed[1] if show and revealed else mask_phone(row.phone),
        phone_revealed=show,
        email=mask_email(row.email),
    )


async def _list_page(
    request: Request,
    settings: Settings,
    admin: AdminRef,
    uow: UnitOfWork,
    page: int,
    *,
    revealed: tuple[int, str] | None = None,
    notice: str = "",
    status_code: int = 200,
) -> Response:
    page = max(page, 1)
    rows, total = await list_users_page(
        uow.repositories.users, PAGE_SIZE, (page - 1) * PAGE_SIZE
    )
    csrf_value, is_new = current_csrf_value(request)
    response = render_page(
        "users_list.html",
        title="Пользователи",
        admin=admin,
        csrf=csrf_value,
        items=[_item(row, revealed) for row in rows],
        total=total,
        page=page,
        has_prev=page > 1,
        has_next=page * PAGE_SIZE < total,
        notice=notice,
        status_code=status_code,
    )
    if is_new:
        attach_csrf_cookie(response, csrf_value, secure=settings.cookie_secure)
    return response


@router.get("/users")
async def users_list(
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
) -> Response:
    return await _list_page(request, settings, admin, uow, page)


@router.post("/users/{user_id}/reveal_phone")
async def users_reveal_phone(
    user_id: int,
    request: Request,
    admin: Annotated[AdminRef, Depends(current_admin)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    page: Annotated[int, Form(ge=1, le=100_000)] = 1,
) -> Response:
    try:
        phone = await reveal_user_phone(
            uow.repositories.users,
            uow.repositories.audit,
            AdminId(admin.id),
            UserId(user_id),
            clock.now(),
        )
    except ConflictError:
        # Анонимизирован или без телефона: журнал не пишем, показываем только сообщение.
        return await _list_page(
            request, settings, admin, uow, page,
            notice="Телефон недоступен: пользователь удалён.", status_code=409,
        )
    await uow.commit()  # журнал фиксируется до того, как номер уйдёт в ответ
    # Только идентификаторы: ни телефона, ни имени (правило 2).
    logger.info(
        "reveal_phone_completed",
        extra={"event": "reveal_phone_completed", "admin_id": admin.id, "user_id": user_id},
    )
    return await _list_page(request, settings, admin, uow, page, revealed=(user_id, phone))
