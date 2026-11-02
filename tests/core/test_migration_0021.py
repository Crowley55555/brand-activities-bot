"""Миграция 0021 (фаза 10b-3, I-2): tg_fsm_state.user_id + partial index.

До 0021: tg_fsm_state не хранил user_id — /forget каскад удалял строки через
LIKE по PK (нестабильно, ядро не знает bot_id/chat_id).
После: `user_id BIGINT NULL` + partial index `tg_fsm_state_user_id`
WHERE user_id IS NOT NULL.

Downgrade: индекс + колонка удаляются (данные без user_id не теряются — они
NULL до миграции и до неё и были).
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic import command

PREVIOUS = "0020_audit_log_jobs"


def _column_exists(engine: Engine, table: str, column: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).first()
    return row is not None


def _index_exists(engine: Engine, index: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :i"),
            {"i": index},
        ).first()
    return row is not None


def test_user_id_column_present_after_upgrade(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """После 0021 колонка tg_fsm_state.user_id существует."""
    assert _column_exists(sync_engine, "tg_fsm_state", "user_id")


def test_partial_index_exists(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """Partial index `tg_fsm_state_user_id` создан."""
    assert _index_exists(sync_engine, "tg_fsm_state_user_id")


def test_insert_with_user_id_and_query(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """INSERT с user_id проходит, SELECT WHERE user_id = X возвращает строку."""
    with sync_engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (created_at, updated_at) VALUES (now(), now()) "
                "RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO tg_fsm_state (key, state, data, updated_at, user_id) "
                "VALUES (:k, 'reg:wait_phone', '{}'::jsonb, now(), :uid)"
            ),
            {"k": "bot:1:2:::default", "uid": int(uid)},
        )
    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM tg_fsm_state WHERE user_id = :uid"),
            {"uid": int(uid)},
        ).scalar_one()
    assert count == 1


def test_insert_with_null_user_id_still_works(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """user_id NULL — валидно (историческая строка до 0021)."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tg_fsm_state (key, state, data, updated_at) "
                "VALUES (:k, NULL, '{}'::jsonb, now())"
            ),
            {"k": "legacy:1"},
        )
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM tg_fsm_state WHERE key = :k"),
            {"k": "legacy:1"},
        ).one()
    assert row.user_id is None


def test_migration_0021_reversible(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """downgrade удаляет колонку и индекс, upgrade возвращает их."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    try:
        command.downgrade(cfg, PREVIOUS)
        assert not _column_exists(sync_engine, "tg_fsm_state", "user_id")
        assert not _index_exists(sync_engine, "tg_fsm_state_user_id")
    finally:
        command.upgrade(cfg, "head")

    assert _column_exists(sync_engine, "tg_fsm_state", "user_id")
    assert _index_exists(sync_engine, "tg_fsm_state_user_id")
