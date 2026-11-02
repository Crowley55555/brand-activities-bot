from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_access.repository import AdminCredentials
from app.core.types.dto import AdminRef, BrandRef
from app.core.types.ids import AdminId, BrandId


class SqlAdminRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_login(self, login: str) -> AdminCredentials | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, login, display_name, password_hash, "
                    "is_active, deleted_at "
                    "FROM admin_users "
                    "WHERE login = :login "
                    "AND is_active = TRUE "
                    "AND deleted_at IS NULL"
                ),
                {"login": login},
            )
        ).one_or_none()
        if row is None:
            return None
        return AdminCredentials(
            id=AdminId(row.id),
            login=row.login,
            display_name=row.display_name,
            password_hash=row.password_hash,
            is_active=bool(row.is_active),
            deleted_at=row.deleted_at,
        )

    async def get_active_by_id(self, admin_id: AdminId) -> AdminRef | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, login, display_name "
                    "FROM admin_users "
                    "WHERE id = :admin_id "
                    "AND is_active = TRUE "
                    "AND deleted_at IS NULL"
                ),
                {"admin_id": int(admin_id)},
            )
        ).one_or_none()
        if row is None:
            return None
        return AdminRef(
            id=AdminId(row.id), login=row.login, display_name=row.display_name
        )

    async def touch_last_login(self, admin_id: AdminId, now: datetime) -> None:
        await self._session.execute(
            text("UPDATE admin_users SET last_login_at = :now WHERE id = :admin_id"),
            {"now": now, "admin_id": int(admin_id)},
        )

    async def list_brands_for_admin(self, admin_id: AdminId) -> list[BrandRef]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT b.id, b.slug, b.display_name, b.tagline "
                    "FROM admin_brands ab "
                    "JOIN admin_users au ON au.id = ab.admin_id "
                    "JOIN brands b ON b.id = ab.brand_id "
                    "WHERE ab.admin_id = :admin_id "
                    "AND au.is_active = TRUE AND au.deleted_at IS NULL "
                    "AND b.is_active = TRUE AND b.deleted_at IS NULL "
                    "ORDER BY b.display_name"
                ),
                {"admin_id": int(admin_id)},
            )
        ).all()
        return [
            BrandRef(
                id=BrandId(row.id),
                slug=row.slug,
                display_name=row.display_name,
                tagline=row.tagline,
            )
            for row in rows
        ]

    async def insert(
        self,
        login: str,
        password_hash: str,
        display_name: str,
        now: datetime,
    ) -> AdminRef | None:
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO admin_users "
                    "(login, password_hash, display_name, created_at, updated_at) "
                    "VALUES (:login, :pwd_hash, :display_name, :now, :now) "
                    "ON CONFLICT (login) DO NOTHING "
                    "RETURNING id, login, display_name"
                ),
                {
                    "login": login,
                    "pwd_hash": password_hash,
                    "display_name": display_name,
                    "now": now,
                },
            )
        ).one_or_none()
        if row is None:
            return None
        return AdminRef(
            id=AdminId(row.id), login=row.login, display_name=row.display_name
        )
