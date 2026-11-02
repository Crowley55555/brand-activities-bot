from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.types.enums import ChannelType
from app.core.types.ids import ConsentId, UserId


@runtime_checkable
class ConsentRepo(Protocol):
    async def has_active(self, user_id: UserId, policy_version: str) -> bool: ...

    async def insert_active(
        self,
        user_id: UserId,
        policy_version: str,
        text_hash: str,
        channel_type: ChannelType,
        now: datetime,
    ) -> ConsentId: ...

    async def revoke_active(
        self, user_id: UserId, policy_version: str, now: datetime
    ) -> bool: ...
