from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.types.dto import UserListRow, UserPii, UserRef
from app.core.types.ids import UserId


@runtime_checkable
class UserRepo(Protocol):
    async def insert_blank(self, now: datetime) -> UserRef: ...

    async def get_by_id(self, user_id: UserId) -> UserRef: ...

    async def touch_last_seen(self, user_id: UserId, now: datetime) -> None: ...

    async def update_pii(
        self,
        user_id: UserId,
        first_name: str,
        last_name: str,
        phone_e164: str,
        email_lower: str,
        now: datetime,
    ) -> UserRef: ...

    async def list_registered(self, limit: int, offset: int) -> list[UserListRow]: ...

    async def count_registered(self) -> int: ...

    async def get_pii(self, user_id: UserId) -> UserPii: ...

    async def anonymize(self, user_id: UserId, now: datetime) -> bool:
        """`/forget` (фаза 10b-3, I-2): обнулить ПДн, проставить anonymized_at.

        True — поля обновлены (пользователь был не анонимизирован).
        False — повторная анонимизация (anonymized_at уже заполнен).
        """
        ...
