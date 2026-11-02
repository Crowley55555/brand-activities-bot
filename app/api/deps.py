from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request

from app.common.errors import Forbidden
from app.core.ports.clock import ClockPort
from app.core.ports.password_hasher import PasswordHasherPort
from app.core.types.dto import AdminRef
from app.db.unit_of_work import UnitOfWork


async def get_uow(request: Request) -> AsyncIterator[UnitOfWork]:
    factory = request.app.state.session_factory
    uow = UnitOfWork(factory)
    async with uow:
        yield uow


def get_clock(request: Request) -> ClockPort:
    clock: ClockPort = request.app.state.clock
    return clock


def get_password_hasher(request: Request) -> PasswordHasherPort:
    hasher: PasswordHasherPort = request.app.state.password_hasher
    return hasher


def current_admin(request: Request) -> AdminRef:
    """Админ, которого поставил AdminSessionMiddleware (ADR-9).

    Второй рубеж правила 10: если маршрут /admin/* каким-то образом выполнился
    без middleware (его убрали из main.py), админ не появится и мы ответим 403,
    а не отдадим страницу.
    """
    admin = getattr(request.state, "admin", None)
    if not isinstance(admin, AdminRef):
        raise Forbidden("admin_session_required")
    return admin
