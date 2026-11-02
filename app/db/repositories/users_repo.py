from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import ConflictError, NotFound
from app.core.types.dto import UserListRow, UserPii, UserRef
from app.core.types.ids import UserId


class SqlUserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_blank(self, now: datetime) -> UserRef:
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO users (created_at, updated_at) "
                    "VALUES (:now, :now) "
                    "RETURNING id, created_at, anonymized_at"
                ),
                {"now": now},
            )
        ).one()
        return UserRef(
            id=UserId(row.id),
            is_registered=False,
            anonymized_at=row.anonymized_at,
            created_at=row.created_at,
        )

    async def get_by_id(self, user_id: UserId) -> UserRef:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, created_at, anonymized_at, "
                    "registered_at IS NOT NULL AS is_registered "
                    "FROM users WHERE id = :uid"
                ),
                {"uid": int(user_id)},
            )
        ).one_or_none()
        if row is None:
            raise NotFound("user_not_found")
        return UserRef(
            id=UserId(row.id),
            is_registered=bool(row.is_registered),
            anonymized_at=row.anonymized_at,
            created_at=row.created_at,
        )

    async def touch_last_seen(self, user_id: UserId, now: datetime) -> None:
        await self._session.execute(
            text("UPDATE users SET last_seen_at = :now WHERE id = :uid"),
            {"now": now, "uid": int(user_id)},
        )

    async def update_pii(
        self,
        user_id: UserId,
        first_name: str,
        last_name: str,
        phone_e164: str,
        email_lower: str,
        now: datetime,
    ) -> UserRef:
        try:
            row = (
                await self._session.execute(
                    text(
                        "UPDATE users SET "
                        "first_name = :fn, "
                        "last_name = :ln, "
                        "phone = :ph, "
                        "email = :em, "
                        "registered_at = COALESCE(registered_at, :now) "
                        "WHERE id = :uid "
                        "RETURNING id, created_at, anonymized_at, "
                        "registered_at IS NOT NULL AS is_registered"
                    ),
                    {
                        "fn": first_name,
                        "ln": last_name,
                        "ph": phone_e164,
                        "em": email_lower,
                        "now": now,
                        "uid": int(user_id),
                    },
                )
            ).one_or_none()
        except IntegrityError as exc:
            constraint = (getattr(exc.orig, "diag", None) and exc.orig.diag.constraint_name) or ""
            if constraint == "users_phone_unique":
                raise ConflictError("phone_taken") from exc
            if constraint == "users_email_unique":
                raise ConflictError("email_taken") from exc
            raise
        if row is None:
            raise NotFound("user_not_found")
        return UserRef(
            id=UserId(row.id),
            is_registered=bool(row.is_registered),
            anonymized_at=row.anonymized_at,
            created_at=row.created_at,
        )

    async def list_registered(self, limit: int, offset: int) -> list[UserListRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, created_at, registered_at, last_seen_at, anonymized_at, "
                    "first_name, last_name, phone, email "
                    "FROM users WHERE registered_at IS NOT NULL "
                    "ORDER BY registered_at DESC, id DESC LIMIT :lim OFFSET :off"
                ),
                {"lim": limit, "off": offset},
            )
        ).all()
        return [
            UserListRow(
                id=UserId(r.id),
                created_at=r.created_at,
                registered_at=r.registered_at,
                last_seen_at=r.last_seen_at,
                anonymized_at=r.anonymized_at,
                first_name=r.first_name,
                last_name=r.last_name,
                phone=r.phone,
                email=r.email,
            )
            for r in rows
        ]

    async def count_registered(self) -> int:
        value = (
            await self._session.execute(
                text("SELECT count(*) FROM users WHERE registered_at IS NOT NULL")
            )
        ).scalar_one()
        return int(value)

    async def get_pii(self, user_id: UserId) -> UserPii:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, first_name, last_name, phone, email, anonymized_at, "
                    "registered_at IS NOT NULL AS is_registered "
                    "FROM users WHERE id = :uid"
                ),
                {"uid": int(user_id)},
            )
        ).one_or_none()
        if row is None or not row.is_registered:
            raise NotFound("user_not_found")
        if row.anonymized_at is not None:
            raise ConflictError("user_anonymized")
        return UserPii(
            id=UserId(row.id),
            first_name=row.first_name,
            last_name=row.last_name,
            phone=row.phone,
            email=row.email,
        )

    async def anonymize(self, user_id: UserId, now: datetime) -> bool:
        """Фаза 10b-3 (/forget, I-2): анонимизация пользователя.

        UPDATE обнуляет `first_name/last_name/phone/email` и ставит `anonymized_at`.
        Канон 02 §9 (`/forget` → анонимизация, не физическое удаление): строка в
        `users` остаётся — на неё ссылаются `attempts`, `rewards`, `audit_log`.
        Повторная анонимизация (`anonymized_at IS NOT NULL`) → False (идемпотентно,
        но service-слой превращает в `ConflictError('already_anonymized')`).
        """
        row = (
            await self._session.execute(
                text(
                    "UPDATE users SET "
                    "first_name = NULL, last_name = NULL, "
                    "phone = NULL, email = NULL, "
                    "anonymized_at = :ts, updated_at = :ts "
                    "WHERE id = :uid AND anonymized_at IS NULL "
                    "RETURNING id"
                ),
                {"ts": now, "uid": int(user_id)},
            )
        ).first()
        return row is not None
