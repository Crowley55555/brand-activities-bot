"""Фаза 10b-3, N-5: cleanup_stale_fsm джоб удаляет протухшие строки tg_fsm_state.

Канон 03 §3: FSM гостя живёт короткое время. ADR-50: раз в тик `scheduler_tick`
зовёт DELETE WHERE updated_at < now - FSM_STALE_HOURS. Защита от висячих
ПДн-данных регистрации и исторических строк без user_id.

Правило 2: scheduler-лог содержит только `event` и `count`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.workers.context import WorkerDeps
from app.workers.scheduler_tick import scheduler_tick
from tests.conftest import ManualClock

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


def _deps() -> WorkerDeps:
    return WorkerDeps(
        files=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        sniffer=None,  # type: ignore[arg-type]
        clock=ManualClock(NOW),
        ugc_bucket="ugc-test",
    )


@pytest.fixture
def ttl_24h(monkeypatch: pytest.MonkeyPatch) -> None:
    """FSM_STALE_HOURS=24 (default, но явно сбрасываем cache)."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("FSM_STALE_HOURS", "24")
    yield
    get_settings.cache_clear()
    monkeypatch.delenv("FSM_STALE_HOURS", raising=False)


def _insert_row(
    engine: Engine,
    key: str,
    updated_at: datetime,
    user_id: int | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tg_fsm_state (key, state, data, updated_at, user_id) "
                "VALUES (:k, 'reg:wait_phone', '{}'::jsonb, :ts, :uid)"
            ),
            {"k": key, "ts": updated_at, "uid": user_id},
        )


def _rows(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {
            str(r.key)
            for r in conn.execute(text("SELECT key FROM tg_fsm_state")).all()
        }


async def test_cleanup_removes_rows_older_than_ttl(
    ttl_24h: None,
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """updated_at = NOW - 25h → удалена; NOW - 1h → остаётся."""
    _insert_row(sync_engine, "stale:1", NOW - timedelta(hours=25))
    _insert_row(sync_engine, "fresh:1", NOW - timedelta(hours=1))

    await scheduler_tick(uow_factory, NOW, _deps())

    assert _rows(sync_engine) == {"fresh:1"}


async def test_cleanup_ignores_user_id_only_uses_updated_at(
    ttl_24h: None,
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """Исторические строки без user_id тоже удаляются по updated_at."""
    _insert_row(sync_engine, "stale:no-user", NOW - timedelta(hours=48), user_id=None)
    _insert_row(sync_engine, "fresh:no-user", NOW - timedelta(hours=1), user_id=None)

    await scheduler_tick(uow_factory, NOW, _deps())

    assert _rows(sync_engine) == {"fresh:no-user"}


async def test_cleanup_empty_table_is_safe(
    ttl_24h: None,
    uow_factory: Any,
    clean_db: None,
    sync_engine: Engine,
) -> None:
    """Пустая таблица → scheduler_tick не падает, count = 0."""
    await scheduler_tick(uow_factory, NOW, _deps())
    assert _rows(sync_engine) == set()
