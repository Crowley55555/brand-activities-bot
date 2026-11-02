from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.common.dummy_hash import DUMMY_HASH
from app.common.errors import ConflictError, ValidationError
from app.core.admin_access.repository import AdminRepo
from app.core.ports.password_hasher import PasswordHasherPort
from app.core.types.dto import AdminRef, BrandRef
from app.core.types.ids import AdminId

MAX_LOGIN_LENGTH: Final = 254
MAX_DISPLAY_NAME_LENGTH: Final = 200
MIN_PASSWORD_LENGTH: Final = 10
MAX_PASSWORD_LENGTH: Final = 1024


@dataclass(frozen=True)
class SessionPayload:
    """Содержимое админ-сессии. Подпись/сериализация - в HTTP-слое (ADR-9)."""

    admin_id: AdminId
    login: str
    issued_at: datetime


async def authenticate(
    repo: AdminRepo,
    login: str,
    password: str,
    hasher: PasswordHasherPort,
) -> AdminRef | None:
    """Админ при верной паре логин/пароль, иначе None (никогда не бросает).

    Одинаковая стоимость во всех ветках отказа: нет логина, админ неактивен
    или удалён, слишком длинный ввод - везде ровно одна верификация хэша
    (против DUMMY_HASH, см. app/common/dummy_hash.py), поэтому по времени
    ответа нельзя понять, существует ли логин.
    """
    if len(login) > MAX_LOGIN_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        await asyncio.to_thread(hasher.verify, "", DUMMY_HASH)
        return None

    record = await repo.find_by_login(login)
    if record is None or not record.is_active or record.deleted_at is not None:
        await asyncio.to_thread(hasher.verify, password, DUMMY_HASH)
        return None

    if not await asyncio.to_thread(hasher.verify, password, record.password_hash):
        return None
    return AdminRef(id=record.id, login=record.login, display_name=record.display_name)


def start_session(admin: AdminRef, now: datetime) -> SessionPayload:
    return SessionPayload(admin_id=admin.id, login=admin.login, issued_at=now)


def verify_csrf(token_cookie: str | None, token_form: str | None) -> bool:
    """Double-submit (ADR-10): значения из cookie и из формы совпадают.

    Пустые значения не принимаются; сравнение по байтам, чтобы не-ASCII ввод
    не ронял compare_digest TypeError'ом.
    """
    if not token_cookie or not token_form:
        return False
    return hmac.compare_digest(
        token_cookie.encode("utf-8"), token_form.encode("utf-8")
    )


async def get_active_admin(repo: AdminRepo, admin_id: AdminId) -> AdminRef | None:
    """Перепроверка сессии на каждом запросе: is_active и deleted_at (ADR-9)."""
    return await repo.get_active_by_id(admin_id)


async def touch_last_login(repo: AdminRepo, admin_id: AdminId, now: datetime) -> None:
    await repo.touch_last_login(admin_id, now)


async def list_authorized_brands(repo: AdminRepo, admin_id: AdminId) -> list[BrandRef]:
    return await repo.list_brands_for_admin(admin_id)


async def create_admin(
    repo: AdminRepo,
    hasher: PasswordHasherPort,
    login: str,
    password: str,
    display_name: str,
    now: datetime,
) -> AdminRef:
    """Создание сотрудника (CLI `python -m app.admin create_user`).

    Без привязок к брендам: права выдаёт владелец отдельно (MVP).
    """
    login = login.strip()
    display_name = display_name.strip()
    if not login or len(login) > MAX_LOGIN_LENGTH or any(c.isspace() for c in login):
        raise ValidationError("login_invalid")
    if not display_name or len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        raise ValidationError("display_name_invalid")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError("password_too_short")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError("password_too_long")

    pwd_hash = await asyncio.to_thread(hasher.hash, password)
    created = await repo.insert(login, pwd_hash, display_name, now)
    if created is None:
        raise ConflictError("admin_login_taken")
    return created
