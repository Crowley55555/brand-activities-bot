from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.types.dto import AdminRef, BrandRef
from app.core.types.ids import AdminId


@dataclass(frozen=True)
class AdminCredentials:
    """Строка admin_users для проверки пароля. Живёт только внутри модуля."""

    id: AdminId
    login: str
    display_name: str
    password_hash: str
    is_active: bool
    deleted_at: datetime | None


@runtime_checkable
class AdminRepo(Protocol):
    async def find_by_login(self, login: str) -> AdminCredentials | None:
        """Админ по логину; активность реализация репозитория фильтрует в SQL,
        а сервис перепроверяет сам (defense in depth)."""
        ...

    async def get_active_by_id(self, admin_id: AdminId) -> AdminRef | None:
        """Только is_active И deleted_at IS NULL, иначе None (ADR-9)."""
        ...

    async def touch_last_login(self, admin_id: AdminId, now: datetime) -> None: ...

    async def list_brands_for_admin(self, admin_id: AdminId) -> list[BrandRef]: ...

    async def insert(
        self,
        login: str,
        password_hash: str,
        display_name: str,
        now: datetime,
    ) -> AdminRef | None:
        """None, если логин уже занят (уникальный индекс admin_users.login)."""
        ...
