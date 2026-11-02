from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.common.errors import ConflictError
from app.core.consent import accept, has_active_consent, require_consent
from app.core.consent.policy import CURRENT_POLICY_VERSION
from app.core.types.enums import ChannelType
from app.core.types.ids import ConsentId, UserId


class FakeConsentRepo:
    def __init__(self) -> None:
        self._active: dict[tuple[int, str], int] = {}
        self._next_id = 1

    async def has_active(self, user_id: UserId, policy_version: str) -> bool:
        return (int(user_id), policy_version) in self._active

    async def insert_active(
        self,
        user_id: UserId,
        policy_version: str,
        text_hash: str,
        channel_type: ChannelType,
        now: datetime,
    ) -> ConsentId:
        key = (int(user_id), policy_version)
        if key in self._active:
            return ConsentId(self._active[key])
        self._active[key] = self._next_id
        self._next_id += 1
        return ConsentId(self._active[key])

    async def revoke_active(
        self, user_id: UserId, policy_version: str, now: datetime
    ) -> bool:
        return self._active.pop((int(user_id), policy_version), None) is not None


async def test_require_consent_raises_without_active_consent() -> None:
    repo = FakeConsentRepo()
    with pytest.raises(ConflictError) as exc:
        await require_consent(repo, UserId(1))
    assert exc.value.code == "consent_required"


async def test_require_consent_passes_with_active_consent() -> None:
    repo = FakeConsentRepo()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    await accept(repo, UserId(1), "telegram", now)
    # не бросает
    await require_consent(repo, UserId(1))


async def test_has_active_consent_boolean() -> None:
    repo = FakeConsentRepo()
    assert await has_active_consent(repo, UserId(1)) is False
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    await accept(repo, UserId(1), "telegram", now)
    assert await has_active_consent(repo, UserId(1)) is True


async def test_accept_is_idempotent_for_same_version() -> None:
    repo = FakeConsentRepo()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    cid1 = await accept(repo, UserId(1), "telegram", now)
    cid2 = await accept(repo, UserId(1), "telegram", now)
    assert cid1 == cid2
    assert CURRENT_POLICY_VERSION == "v1"
