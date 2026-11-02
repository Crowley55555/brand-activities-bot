from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.types.dto import ChannelRef
from app.core.types.enums import ChannelType
from app.core.types.ids import UserId


@runtime_checkable
class ChannelAccountRepo(Protocol):
    async def find_active(
        self, channel_type: ChannelType, external_id: str
    ) -> ChannelRef | None: ...

    async def insert(
        self,
        user_id: UserId,
        channel_type: ChannelType,
        external_id: str,
        external_username: str | None,
        now: datetime,
    ) -> ChannelRef | None: ...

    async def find_active_by_user(
        self, user_id: UserId, channel_type: ChannelType
    ) -> ChannelRef | None:
        """Действующий канал гостя (unlinked_at IS NULL). Нужен notify-обработчикам (ADR-31)."""
        ...

    async def mark_unlinked(
        self, channel_type: ChannelType, external_id: str, now: datetime
    ) -> bool:
        """Блок бота: unlinked_at = now (один раз). True - помечен сейчас, False - уже помечен."""
        ...
