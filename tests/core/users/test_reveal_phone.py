from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.common.errors import ConflictError
from app.core.types.dto import UserPii
from app.core.types.ids import AdminId, UserId
from app.core.users.service import reveal_user_phone

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class FakeUsers:
    def __init__(self, anonymized: bool) -> None:
        self._anonymized = anonymized

    async def get_pii(self, user_id: UserId) -> UserPii:
        if self._anonymized:
            raise ConflictError("user_anonymized")
        return UserPii(
            id=user_id, first_name="А", last_name="Б", phone="+79161234567", email="a@b.ru"
        )


class FakeAudit:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def insert(self, *args: Any) -> None:
        self.calls.append(args)


async def test_reveal_phone_returns_e164_and_writes_audit() -> None:
    audit = FakeAudit()
    phone = await reveal_user_phone(
        FakeUsers(False), audit, AdminId(3), UserId(7), NOW  # type: ignore[arg-type]
    )
    assert phone == "+79161234567"
    assert audit.calls == [(3, "reveal_user_phone", "users", 7, None, NOW)]


async def test_reveal_phone_user_anonymized_raises_without_audit() -> None:
    audit = FakeAudit()
    with pytest.raises(ConflictError):
        await reveal_user_phone(
            FakeUsers(True), audit, AdminId(3), UserId(7), NOW  # type: ignore[arg-type]
        )
    assert audit.calls == []
