from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types.enums import ChannelType
from app.core.types.ids import ConsentId, UserId


class SqlConsentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_active(self, user_id: UserId, policy_version: str) -> bool:
        row = (
            await self._session.execute(
                text(
                    "SELECT 1 FROM consents "
                    "WHERE user_id = :uid "
                    "AND policy_version = :ver "
                    "AND revoked_at IS NULL "
                    "LIMIT 1"
                ),
                {"uid": int(user_id), "ver": policy_version},
            )
        ).first()
        return row is not None

    async def insert_active(
        self,
        user_id: UserId,
        policy_version: str,
        text_hash: str,
        channel_type: ChannelType,
        now: datetime,
    ) -> ConsentId:
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO consents "
                    "(user_id, policy_version, text_hash, "
                    " accepted_at, channel_type, created_at, updated_at) "
                    "VALUES (:uid, :ver, :h, :ts, :ct, :ts, :ts) "
                    "ON CONFLICT (user_id, policy_version) "
                    "WHERE revoked_at IS NULL DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "uid": int(user_id),
                    "ver": policy_version,
                    "h": text_hash,
                    "ct": channel_type,
                    "ts": now,
                },
            )
        ).one_or_none()
        if row is not None:
            return ConsentId(row.id)
        existing = (
            await self._session.execute(
                text(
                    "SELECT id FROM consents "
                    "WHERE user_id = :uid "
                    "AND policy_version = :ver "
                    "AND revoked_at IS NULL"
                ),
                {"uid": int(user_id), "ver": policy_version},
            )
        ).one()
        return ConsentId(existing.id)

    async def revoke_active(
        self, user_id: UserId, policy_version: str, now: datetime
    ) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE consents SET revoked_at = :ts "
                "WHERE user_id = :uid "
                "AND policy_version = :ver "
                "AND revoked_at IS NULL"
            ),
            {"uid": int(user_id), "ver": policy_version, "ts": now},
        )
        return (result.rowcount or 0) > 0
