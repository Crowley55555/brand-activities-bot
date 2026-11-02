from __future__ import annotations

from datetime import datetime

from app.common.errors import ConflictError
from app.core.consent.policy import CURRENT_POLICY_TEXT_HASH, CURRENT_POLICY_VERSION
from app.core.consent.repository import ConsentRepo
from app.core.types.enums import ChannelType
from app.core.types.ids import ConsentId, UserId


async def has_active_consent(repo: ConsentRepo, user_id: UserId) -> bool:
    return await repo.has_active(user_id, CURRENT_POLICY_VERSION)


async def require_consent(repo: ConsentRepo, user_id: UserId) -> None:
    """Guard: бросает ConflictError('consent_required'), если согласия нет.

    Правило 1 (consent-gate): любая запись ПДн в БД сначала зовёт этот guard.
    """
    if not await repo.has_active(user_id, CURRENT_POLICY_VERSION):
        raise ConflictError("consent_required")


async def accept(
    repo: ConsentRepo,
    user_id: UserId,
    channel_type: ChannelType,
    now: datetime,
) -> ConsentId:
    """Идемпотентно принимает актуальную политику."""
    return await repo.insert_active(
        user_id=user_id,
        policy_version=CURRENT_POLICY_VERSION,
        text_hash=CURRENT_POLICY_TEXT_HASH,
        channel_type=channel_type,
        now=now,
    )


async def revoke(repo: ConsentRepo, user_id: UserId, now: datetime) -> None:
    """Отзывает действующее согласие. Audit-строка — в будущих фазах."""
    await repo.revoke_active(user_id, CURRENT_POLICY_VERSION, now)
