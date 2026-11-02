from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

if TYPE_CHECKING:
    from app.db.unit_of_work import UnitOfWork

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Общие интеграционные фикстуры (Postgres в docker + ASGI-клиент) живут здесь,
# в корне tests/, чтобы tests/api и tests/admin делили ОДИН контейнер на сессию.
# Повторный импорт тех же фикстур из соседнего conftest создал бы второй
# FixtureDef и второй контейнер с тем же именем.
TEST_DB_URL = "postgresql+psycopg://postgres:postgres@localhost:55432/postgres"
TEST_HMAC = "test-hmac-value-xxxxxxxxxxxxxxxx"
TEST_ADMIN_KEY = "test-admin-session-key-value-xxxxxxxxxxxxxxxx"
PG_CONTAINER = "brand_activities-test-pg"


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _postgres_up() -> None:
    subprocess.run(
        ["docker", "rm", "-f", PG_CONTAINER],
        capture_output=True,
        text=True,
    )
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            PG_CONTAINER,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-p",
            "55432:5432",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"failed to start postgres: {r.stderr}")

    for _ in range(60):
        pr = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "pg_isready", "-U", "postgres"],
            capture_output=True,
            text=True,
        )
        if pr.returncode == 0:
            time.sleep(0.5)
            return
        time.sleep(1)
    raise RuntimeError("postgres container did not become ready in 60s")


def _postgres_down() -> None:
    subprocess.run(
        ["docker", "rm", "-f", PG_CONTAINER],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    if not _docker_available():
        pytest.skip("docker required for integration test")

    _postgres_up()
    os.environ["DATABASE_URL"] = TEST_DB_URL
    os.environ["INTERNAL_API_AUTH_SECRET"] = TEST_HMAC
    os.environ["ADMIN_SESSION_SECRET"] = TEST_ADMIN_KEY
    os.environ["COOKIE_SECURE"] = "false"

    from alembic.config import Config

    from alembic import command
    from app.config import get_settings

    get_settings.cache_clear()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")

    try:
        yield TEST_DB_URL
    finally:
        _postgres_down()


@pytest.fixture(scope="session")
def sync_engine(pg_url: str) -> Iterator[Engine]:
    engine = create_engine(pg_url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


class ManualClock:
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = moment


@pytest.fixture
def manual_clock() -> ManualClock:
    return ManualClock(datetime(2026, 10, 12, 5, 0, tzinfo=UTC))


@pytest.fixture
def clean_db(sync_engine: Engine) -> Iterator[None]:
    """Чистит данные гостей и прохождений перед тестом.

    TRUNCATE users ... CASCADE уносит всё, что на них ссылается (attempts,
    survey_answers, rewards, consents, channel_accounts, audit_log ...).
    Справочники (brands, activities, survey_questions, product_recommendations)
    остаются: их наполняют сид-миграции, тесты на них опираются.
    """
    with sync_engine.begin() as conn:
        # jobs без FK на гостей: CASCADE её не заденет, чистим явно (фаза 3a).
        # tg_fsm_state без FK — чистим явно (фаза 10a, C-4).
        conn.execute(
            text(
                "TRUNCATE TABLE channel_accounts, consents, users, jobs, "
                "tg_fsm_state RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def uow_factory(pg_url: str) -> AsyncIterator[Callable[[], UnitOfWork]]:
    """Фабрика UnitOfWork на отдельном async-движке теста.

    Каждый `async with uow_factory() as uow` - отдельная транзакция, как один
    HTTP-запрос: commit на выходе без исключения, rollback при исключении.
    """
    from app.db.engine import create_engine_from_url
    from app.db.session import make_session_factory
    from app.db.unit_of_work import UnitOfWork

    engine = create_engine_from_url(pg_url)
    session_factory = make_session_factory(engine)
    try:
        yield lambda: UnitOfWork(session_factory)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def api_client(
    pg_url: str,
    manual_clock: ManualClock,
) -> AsyncIterator[AsyncClient]:
    from app.db.engine import create_engine_from_url
    from app.db.session import make_session_factory
    from app.main import app

    engine = create_engine_from_url(pg_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.clock = manual_clock

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
