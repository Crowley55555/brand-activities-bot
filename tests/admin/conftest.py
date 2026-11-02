from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.engine import Engine

if TYPE_CHECKING:
    from app.core.ports.password_hasher import PasswordHasherPort

SESSION_COOKIE = "admin_session"
CSRF_COOKIE = "admin_csrf"

# Имена без суффиксов secret/password/token — см. scripts/check_secrets.py.
ARGON_PLAIN = "correct-horse-battery-staple"


@dataclass(frozen=True)
class CreatedAdmin:
    id: int
    login: str
    plain: str


@dataclass(frozen=True)
class ParsedCookie:
    name: str
    value: str
    attrs: dict[str, str]  # ключи в нижнем регистре; флаги (HttpOnly, Secure) -> ""

    def has(self, flag: str) -> bool:
        return flag.lower() in self.attrs


def parse_set_cookie(header: str) -> ParsedCookie:
    parts = [p.strip() for p in header.split(";")]
    name, _, value = parts[0].partition("=")
    attrs: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        attrs[key.strip().lower()] = val.strip()
    return ParsedCookie(name=name.strip(), value=value.strip(), attrs=attrs)


def find_set_cookie(resp: Response, name: str) -> ParsedCookie | None:
    for header in resp.headers.get_list("set-cookie"):
        parsed = parse_set_cookie(header)
        if parsed.name == name:
            return parsed
    return None


def login_form(login: str, pw: str, csrf: str, next_url: str = "") -> dict[str, str]:
    return {"login": login, "password": pw, "_csrf": csrf, "next": next_url}


async def fetch_csrf(client: AsyncClient) -> str:
    """GET /admin/login выставляет CSRF-cookie; возвращаем её значение."""
    resp = await client.get("/admin/login")
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf, "GET /admin/login не выставил CSRF-cookie"
    return csrf


async def sign_in(client: AsyncClient, admin: CreatedAdmin) -> Response:
    csrf = await fetch_csrf(client)
    resp = await client.post(
        "/admin/login", data=login_form(admin.login, admin.plain, csrf)
    )
    assert resp.status_code == 302, resp.text
    return resp


@pytest.fixture
def override_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Подменяет env и сбрасывает lru_cache настроек; на выходе возвращает как было."""
    from app.config import get_settings

    def _apply(**env: str) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield _apply
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _truncate_admin(sync_engine: Engine) -> Iterator[None]:
    with sync_engine.begin() as conn:
        conn.execute(
            text("TRUNCATE TABLE admin_brands, admin_users RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture
def password_hasher() -> PasswordHasherPort:
    from app.adapters.password import build

    return build()


@pytest.fixture
def admin_user_factory(
    sync_engine: Engine, password_hasher: PasswordHasherPort
) -> Callable[..., CreatedAdmin]:
    def _create(
        *,
        login: str = "admin@test",
        plain: str = ARGON_PLAIN,
        display_name: str = "Тестовый админ",
        is_active: bool = True,
        deleted: bool = False,
    ) -> CreatedAdmin:
        pwd_hash = password_hasher.hash(plain)
        with sync_engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO admin_users "
                    "(login, password_hash, display_name, is_active, deleted_at) "
                    "VALUES (:login, :pwd_hash, :display_name, :is_active, "
                    "CASE WHEN :deleted THEN now() ELSE NULL END) "
                    "RETURNING id"
                ),
                {
                    "login": login,
                    "pwd_hash": pwd_hash,
                    "display_name": display_name,
                    "is_active": is_active,
                    "deleted": deleted,
                },
            ).one()
        return CreatedAdmin(id=int(row.id), login=login, plain=plain)

    return _create


@pytest_asyncio.fixture
async def admin_client(
    api_client: AsyncClient, password_hasher: PasswordHasherPort
) -> AsyncClient:
    """api_client + адаптер хэширования в app.state (lifespan в тестах не идёт)."""
    from app.main import app

    app.state.password_hasher = password_hasher
    return api_client
