"""Юнит-тесты core.admin_access (T6-T7): без БД и без docker."""

from __future__ import annotations


def test_argon2_verify_round_trip() -> None:
    """T6."""
    from app.adapters.password import build

    hasher = build()
    pwd_hash = hasher.hash("plain")

    assert pwd_hash != "plain"
    assert hasher.verify("plain", pwd_hash) is True
    assert hasher.verify("wrong", pwd_hash) is False


async def test_authenticate_rejects_inactive_admin() -> None:
    """T7: деактивированный админ -> None, исключения нет."""
    from app.adapters.password import build
    from app.core.admin_access import authenticate
    from app.core.admin_access.repository import AdminCredentials
    from app.core.types.ids import AdminId

    hasher = build()
    row = AdminCredentials(
        id=AdminId(1),
        login="admin@test",
        display_name="Админ",
        password_hash=hasher.hash("some-plain"),
        is_active=False,
        deleted_at=None,
    )

    class FakeAdminRepo:
        async def find_by_login(self, login: str) -> AdminCredentials | None:
            return row if login == row.login else None

    result = await authenticate(FakeAdminRepo(), "admin@test", "some-plain", hasher)

    assert result is None
