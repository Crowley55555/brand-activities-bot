"""core.admin_access: тайминг-защита, CSRF-сравнение, сессия, создание (без БД)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.adapters.password import build
from app.common.errors import ConflictError, ValidationError
from app.core.admin_access import (
    AdminCredentials,
    authenticate,
    create_admin,
    start_session,
    verify_csrf,
)
from app.core.admin_access.service import (
    DUMMY_HASH,
    MAX_LOGIN_LENGTH,
    MAX_PASSWORD_LENGTH,
)
from app.core.types.dto import AdminRef
from app.core.types.ids import AdminId

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
GOOD_PLAIN = "correct-horse-battery-staple"


class SpyHasher:
    """Считает вызовы verify и запоминает, против какого хэша сверяли."""

    def __init__(self, result: bool) -> None:
        self._result = result
        self.verify_calls: list[tuple[str, str]] = []

    def hash(self, plain: str) -> str:
        return f"hashed::{plain}"

    def verify(self, plain: str, hashed: str) -> bool:
        self.verify_calls.append((plain, hashed))
        return self._result


class FakeAdminRepo:
    def __init__(self, record: AdminCredentials | None = None) -> None:
        self._record = record
        self.inserted: list[tuple[str, str, str]] = []
        self.taken = False

    async def find_by_login(self, login: str) -> AdminCredentials | None:
        if self._record is not None and self._record.login == login:
            return self._record
        return None

    async def insert(
        self, login: str, password_hash: str, display_name: str, now: datetime
    ) -> AdminRef | None:
        if self.taken:
            return None
        self.inserted.append((login, password_hash, display_name))
        return AdminRef(id=AdminId(42), login=login, display_name=display_name)


def _record(
    *,
    is_active: bool = True,
    deleted_at: datetime | None = None,
    password_hash: str = "stored-hash",
) -> AdminCredentials:
    return AdminCredentials(
        id=AdminId(1),
        login="admin@test",
        display_name="Админ",
        password_hash=password_hash,
        is_active=is_active,
        deleted_at=deleted_at,
    )


# ---- authenticate ----------------------------------------------------------


async def test_authenticate_success_returns_ref_without_hash() -> None:
    hasher = SpyHasher(result=True)

    ref = await authenticate(FakeAdminRepo(_record()), "admin@test", "any", hasher)

    assert ref == AdminRef(id=AdminId(1), login="admin@test", display_name="Админ")
    assert hasher.verify_calls == [("any", "stored-hash")]
    assert not hasattr(ref, "password_hash")


async def test_authenticate_wrong_password_returns_none() -> None:
    hasher = SpyHasher(result=False)

    assert await authenticate(FakeAdminRepo(_record()), "admin@test", "bad", hasher) is None
    assert hasher.verify_calls == [("bad", "stored-hash")]


async def test_unknown_login_still_spends_one_dummy_verification() -> None:
    """Защита от user enumeration по времени: ветка «нет такого логина»
    тратит ту же Argon2-верификацию, что и ветка «неверный пароль»."""
    hasher = SpyHasher(result=True)  # даже «успешный» verify не должен пустить

    assert await authenticate(FakeAdminRepo(None), "nobody", "whatever", hasher) is None
    assert hasher.verify_calls == [("whatever", DUMMY_HASH)]


@pytest.mark.parametrize(
    "state",
    [{"is_active": False}, {"deleted_at": NOW}, {"is_active": False, "deleted_at": NOW}],
)
async def test_unusable_admin_gets_dummy_verification_not_the_real_hash(
    state: dict[str, bool | datetime],
) -> None:
    hasher = SpyHasher(result=True)

    result = await authenticate(
        FakeAdminRepo(_record(**state)), "admin@test", "any", hasher
    )

    assert result is None
    assert hasher.verify_calls == [("any", DUMMY_HASH)], "реальный хэш неактивного не трогаем"


@pytest.mark.parametrize(
    ("login", "plain"),
    [("x" * (MAX_LOGIN_LENGTH + 1), "ok"), ("admin@test", "p" * (MAX_PASSWORD_LENGTH + 1))],
)
async def test_oversized_input_is_refused_after_one_dummy_verification(
    login: str, plain: str
) -> None:
    hasher = SpyHasher(result=True)

    assert await authenticate(FakeAdminRepo(_record()), login, plain, hasher) is None
    assert hasher.verify_calls == [("", DUMMY_HASH)]


async def test_authenticate_with_real_argon2_round_trip() -> None:
    hasher = build()
    repo = FakeAdminRepo(_record(password_hash=hasher.hash(GOOD_PLAIN)))

    assert await authenticate(repo, "admin@test", GOOD_PLAIN, hasher) is not None
    assert await authenticate(repo, "admin@test", GOOD_PLAIN + "x", hasher) is None
    assert await authenticate(repo, "nobody", GOOD_PLAIN, hasher) is None


def test_dummy_hash_matches_hasher_parameters() -> None:
    """Если argon2-cffi сменит дефолты, DUMMY_HASH надо перегенерировать:
    иначе «неизвестный логин» начнёт отвечать быстрее/медленнее настоящего."""

    def params(encoded: str) -> str:
        match = re.fullmatch(r"\$argon2id\$v=\d+\$(m=\d+,t=\d+,p=\d+)\$[^$]+\$[^$]+", encoded)
        assert match is not None, encoded
        return match.group(1)

    assert params(DUMMY_HASH) == params(build().hash("probe"))


def test_dummy_hash_verifies_nothing_we_can_guess() -> None:
    hasher = build()

    assert not any(hasher.verify(guess, DUMMY_HASH) for guess in ("", "admin", GOOD_PLAIN))


# ---- сессия и CSRF ---------------------------------------------------------


def test_start_session_carries_id_login_and_issue_time() -> None:
    admin = AdminRef(id=AdminId(5), login="a@test", display_name="A")

    payload = start_session(admin, NOW)

    assert (payload.admin_id, payload.login, payload.issued_at) == (AdminId(5), "a@test", NOW)


@pytest.mark.parametrize(
    ("cookie_value", "form_value", "expected"),
    [
        ("a" * 64, "a" * 64, True),
        ("a" * 64, "b" * 64, False),
        ("a" * 64, "a" * 63, False),
        ("", "", False),
        (None, None, False),
        (None, "a" * 64, False),
        ("a" * 64, None, False),
        ("пароль", "пароль", True),  # не-ASCII не роняет сравнение TypeError'ом
        ("пароль", "пароль!", False),
    ],
)
def test_verify_csrf_table(
    cookie_value: str | None, form_value: str | None, expected: bool
) -> None:
    assert verify_csrf(cookie_value, form_value) is expected


# ---- create_admin (CLI) ----------------------------------------------------


async def test_create_admin_hashes_password_and_trims_input() -> None:
    repo = FakeAdminRepo()
    hasher = build()

    ref = await create_admin(repo, hasher, "  boss@test ", GOOD_PLAIN, " Босс ", NOW)

    assert (ref.login, ref.display_name) == ("boss@test", "Босс")
    (login, stored, display_name) = repo.inserted[0]
    assert (login, display_name) == ("boss@test", "Босс")
    assert stored.startswith("$argon2id$") and GOOD_PLAIN not in stored
    assert hasher.verify(GOOD_PLAIN, stored)


@pytest.mark.parametrize(
    ("login", "plain", "display_name", "code"),
    [
        ("", GOOD_PLAIN, "A", "login_invalid"),
        ("two words", GOOD_PLAIN, "A", "login_invalid"),
        ("x" * (MAX_LOGIN_LENGTH + 1), GOOD_PLAIN, "A", "login_invalid"),
        ("a@test", GOOD_PLAIN, "   ", "display_name_invalid"),
        ("a@test", "short", "A", "password_too_short"),
        ("a@test", "p" * (MAX_PASSWORD_LENGTH + 1), "A", "password_too_long"),
    ],
)
async def test_create_admin_validates_input_before_touching_storage(
    login: str, plain: str, display_name: str, code: str
) -> None:
    repo = FakeAdminRepo()

    with pytest.raises(ValidationError) as exc:
        await create_admin(repo, build(), login, plain, display_name, NOW)

    assert exc.value.code == code
    assert repo.inserted == []


async def test_create_admin_reports_taken_login_as_conflict() -> None:
    repo = FakeAdminRepo()
    repo.taken = True

    with pytest.raises(ConflictError) as exc:
        await create_admin(repo, build(), "a@test", GOOD_PLAIN, "A", NOW)

    assert exc.value.code == "admin_login_taken"
