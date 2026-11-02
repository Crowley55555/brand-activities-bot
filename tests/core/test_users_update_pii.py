from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.common.errors import ConflictError
from app.core.consent import accept
from app.core.types.dto import UserRef
from app.core.types.enums import ChannelType
from app.core.types.ids import ConsentId, UserId
from app.core.users import update_pii


class FakeUserRepo:
    def __init__(self) -> None:
        self.last: dict[str, object] | None = None

    async def insert_blank(self, now: datetime) -> UserRef:  # pragma: no cover
        raise NotImplementedError

    async def get_by_id(self, user_id: UserId) -> UserRef:  # pragma: no cover
        raise NotImplementedError

    async def touch_last_seen(
        self, user_id: UserId, now: datetime
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def update_pii(
        self,
        user_id: UserId,
        first_name: str,
        last_name: str,
        phone_e164: str,
        email_lower: str,
        now: datetime,
    ) -> UserRef:
        self.last = {
            "user_id": int(user_id),
            "first_name": first_name,
            "last_name": last_name,
            "phone_e164": phone_e164,
            "email_lower": email_lower,
        }
        return UserRef(
            id=user_id,
            is_registered=True,
            anonymized_at=None,
            created_at=now,
        )


class FakeConsentRepo:
    def __init__(self) -> None:
        self._active: set[tuple[int, str]] = set()

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
        self._active.add((int(user_id), policy_version))
        return ConsentId(1)

    async def revoke_active(
        self, user_id: UserId, policy_version: str, now: datetime
    ) -> bool:  # pragma: no cover
        return False


async def test_update_pii_rejects_without_consent() -> None:
    users = FakeUserRepo()
    consents = FakeConsentRepo()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    with pytest.raises(ConflictError) as exc:
        await update_pii(
            users=users,
            consents=consents,
            user_id=UserId(1),
            first_name="Иван",
            last_name="Петров",
            phone_e164="+79001234567",
            email_lower="ivan@example.com",
            now=now,
        )
    assert exc.value.code == "consent_required"
    assert users.last is None  # ПДн не записались


async def test_update_pii_persists_after_accept() -> None:
    users = FakeUserRepo()
    consents = FakeConsentRepo()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    await accept(consents, UserId(1), "telegram", now)
    user_ref = await update_pii(
        users=users,
        consents=consents,
        user_id=UserId(1),
        first_name="Иван",
        last_name="Петров",
        phone_e164="+79001234567",
        email_lower="ivan@example.com",
        now=now,
    )
    assert users.last == {
        "user_id": 1,
        "first_name": "Иван",
        "last_name": "Петров",
        "phone_e164": "+79001234567",
        "email_lower": "ivan@example.com",
    }
    assert user_ref.is_registered is True
