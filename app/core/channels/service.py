from __future__ import annotations

from datetime import datetime

from app.core.channels.repository import ChannelAccountRepo
from app.core.types.dto import ChannelRef, UserRef
from app.core.types.enums import ChannelType
from app.core.users.repository import UserRepo


async def mark_unlinked(
    repo: ChannelAccountRepo, channel_type: ChannelType, external_id: str, now: datetime
) -> bool:
    """Блок бота (ADR-32, канон 04 §3.3): канал помечается unlinked_at, гостю больше не пишем."""
    return await repo.mark_unlinked(channel_type, external_id, now)


async def lookup_user(
    repo: ChannelAccountRepo, channel_type: ChannelType, external_id: str
) -> ChannelRef | None:
    return await repo.find_active(channel_type, external_id)


async def ensure_account(
    users: UserRepo,
    channels: ChannelAccountRepo,
    channel_type: ChannelType,
    external_id: str,
    external_username: str | None,
    now: datetime,
) -> tuple[UserRef, bool]:
    existing = await channels.find_active(channel_type, external_id)
    if existing is not None:
        await users.touch_last_seen(existing.user_id, now)
        user = await users.get_by_id(existing.user_id)
        return user, False

    user = await users.insert_blank(now)
    channel = await channels.insert(
        user.id, channel_type, external_id, external_username, now
    )
    if channel is not None:
        return user, True

    existing = await channels.find_active(channel_type, external_id)
    assert existing is not None
    found = await users.get_by_id(existing.user_id)
    return found, False
