from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types.dto import ChannelRef
from app.core.types.enums import ChannelType
from app.core.types.ids import ChannelAccountId, UserId


class SqlChannelAccountRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active_by_user(
        self, user_id: UserId, channel_type: ChannelType
    ) -> ChannelRef | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, user_id, channel_type, external_id "
                    "FROM channel_accounts "
                    "WHERE user_id = :uid AND channel_type = :ct AND unlinked_at IS NULL "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"uid": int(user_id), "ct": channel_type},
            )
        ).one_or_none()
        if row is None:
            return None
        return ChannelRef(
            id=ChannelAccountId(int(row.id)),
            user_id=UserId(int(row.user_id)),
            channel_type=cast(ChannelType, row.channel_type),
            external_id=str(row.external_id),
        )

    async def mark_unlinked(
        self, channel_type: ChannelType, external_id: str, now: datetime
    ) -> bool:
        row = (
            await self._session.execute(
                text(
                    "UPDATE channel_accounts SET unlinked_at = :ts, updated_at = :ts "
                    "WHERE channel_type = :ct AND external_id = :eid AND unlinked_at IS NULL "
                    "RETURNING id"
                ),
                {"ts": now, "ct": channel_type, "eid": external_id},
            )
        ).first()
        return row is not None

    async def find_active(
        self, channel_type: ChannelType, external_id: str
    ) -> ChannelRef | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, user_id, channel_type, external_id "
                    "FROM channel_accounts "
                    "WHERE channel_type = :ct AND external_id = :eid "
                    "AND unlinked_at IS NULL"
                ),
                {"ct": channel_type, "eid": external_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return ChannelRef(
            id=ChannelAccountId(row.id),
            user_id=UserId(row.user_id),
            channel_type=cast(ChannelType, row.channel_type),
            external_id=row.external_id,
        )

    async def insert(
        self,
        user_id: UserId,
        channel_type: ChannelType,
        external_id: str,
        external_username: str | None,
        now: datetime,
    ) -> ChannelRef | None:
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(user_id, channel_type, external_id, external_username, "
                    "linked_at, created_at, updated_at) "
                    "VALUES (:uid, :ct, :eid, :un, :now, :now, :now) "
                    "ON CONFLICT (channel_type, external_id) "
                    "WHERE unlinked_at IS NULL DO NOTHING "
                    "RETURNING id, user_id, channel_type, external_id"
                ),
                {
                    "uid": int(user_id),
                    "ct": channel_type,
                    "eid": external_id,
                    "un": external_username,
                    "now": now,
                },
            )
        ).one_or_none()
        if row is None:
            return None
        return ChannelRef(
            id=ChannelAccountId(row.id),
            user_id=UserId(row.user_id),
            channel_type=cast(ChannelType, row.channel_type),
            external_id=row.external_id,
        )
