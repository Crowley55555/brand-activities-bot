"""Миграция 0019 (фаза 10b-1, C-1): is_bearer + OTP-колонки на rewards.

Канон 02 §7, 03 §1 (rewards), 04 §1.2, 09 §3. Проверяем:
* три колонки появляются (is_bearer NOT NULL, redeem_otp TEXT NULL,
  redeem_otp_expires_at TIMESTAMPTZ NULL);
* partial unique index `rewards_active_otp` присутствует (WHERE redeem_otp IS NOT NULL
  AND redeemed_at IS NULL);
* downgrade чистит всё и восстанавливается upgrade'ом;
* CHECK `rewards_bearer_no_otp` блокирует вставку predъявительской награды с OTP.

База тестов общая для сессии: каждый цикл возвращает её в head (`finally`).
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from alembic import command

PREVIOUS = "0018_tg_fsm_state"
INDEX_NAME = "rewards_active_otp"
CHECK_BEARER = "rewards_bearer_no_otp"
CHECK_PAIR = "rewards_otp_pair"


def _column_info(engine: Engine, column: str) -> tuple[str, bool]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'rewards' AND column_name = :col"
            ),
            {"col": column},
        ).one_or_none()
    if row is None:
        return ("", "")  # отсутствует
    return (row.data_type, row.is_nullable)


def _has_index(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT 1 FROM pg_indexes "
                "WHERE tablename = 'rewards' AND indexname = :name"
            ),
            {"name": name},
        ).scalar_one_or_none()
    return value is not None


def _index_predicate(engine: Engine, name: str) -> str:
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'rewards' AND indexname = :name"
            ),
            {"name": name},
        ).scalar_one()
    return str(value)


def _has_check(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :name AND contype = 'c'"
            ),
            {"name": name},
        ).scalar_one_or_none()
    return value is not None


def test_otp_columns_added(pg_url: str, sync_engine: Engine, clean_db: None) -> None:
    """На head есть is_bearer NOT NULL, redeem_otp NULL, redeem_otp_expires_at NULL."""
    is_bearer = _column_info(sync_engine, "is_bearer")
    assert is_bearer == ("boolean", "NO"), f"is_bearer: {is_bearer}"

    otp = _column_info(sync_engine, "redeem_otp")
    assert otp == ("text", "YES"), f"redeem_otp: {otp}"

    exp = _column_info(sync_engine, "redeem_otp_expires_at")
    assert exp == ("timestamp with time zone", "YES"), f"redeem_otp_expires_at: {exp}"


def test_otp_unique_partial_index_exists(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """Индекс rewards_active_otp - UNIQUE WHERE redeem_otp IS NOT NULL AND redeemed_at IS NULL."""
    assert _has_index(sync_engine, INDEX_NAME)
    definition = _index_predicate(sync_engine, INDEX_NAME)
    assert "UNIQUE" in definition.upper(), definition
    assert "redeem_otp" in definition
    # В indexdef Postgres может приводить к verbose-форме: достаточно проверить
    # присутствие ключевых фрагментов предиката.
    assert "redeem_otp IS NOT NULL" in definition, definition
    assert "redeemed_at IS NULL" in definition, definition


def test_migration_0019_reversible(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """downgrade чистит все 3 колонки, индекс и CHECK; upgrade восстанавливает."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    try:
        command.downgrade(cfg, PREVIOUS)

        assert _column_info(sync_engine, "is_bearer") == ("", "")
        assert _column_info(sync_engine, "redeem_otp") == ("", "")
        assert _column_info(sync_engine, "redeem_otp_expires_at") == ("", "")
        assert not _has_index(sync_engine, INDEX_NAME)
        assert not _has_check(sync_engine, CHECK_BEARER)
        assert not _has_check(sync_engine, CHECK_PAIR)
    finally:
        # База общая для сессии: при сбое вернём схему в head.
        command.upgrade(cfg, "head")

    # После upgrade - всё на месте.
    assert _column_info(sync_engine, "is_bearer") == ("boolean", "NO")
    assert _column_info(sync_engine, "redeem_otp") == ("text", "YES")
    assert _column_info(sync_engine, "redeem_otp_expires_at") == (
        "timestamp with time zone",
        "YES",
    )
    assert _has_index(sync_engine, INDEX_NAME)
    assert _has_check(sync_engine, CHECK_BEARER)
    assert _has_check(sync_engine, CHECK_PAIR)


def test_otp_check_bearer_blocks_promocode(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """CHECK rewards_bearer_no_otp не даёт поставить OTP у is_bearer=true (promocode).

    Способ: пытаемся напрямую INSERT промокода с is_bearer=true и OTP - IntegrityError.
    Это дешевле, чем создавать весь attempt-пайплайн: мы проверяем именно CHECK.
    """
    # Создадим минимально нужный attempt (пустая users-строка в clean_db уже допустима).
    with sync_engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (first_name, last_name, phone, email, "
                "registered_at, last_seen_at) "
                "VALUES (NULL, NULL, NULL, NULL, NULL, NULL) RETURNING id"
            )
        ).scalar_one()
        channel_id = conn.execute(
            text(
                "INSERT INTO channel_accounts (user_id, channel_type, external_id, "
                "external_username, linked_at) "
                "VALUES (:u, 'telegram', 'test-0019', NULL, now()) RETURNING id"
            ),
            {"u": user_id},
        ).scalar_one()
        # Любая активность кандидата - возьмём опрос Бренд A (seed 0003).
        activity_id = conn.execute(
            text("SELECT id FROM activities WHERE slug = 'brand_a_skin_survey'")
        ).scalar_one()
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts (user_id, activity_id, channel_account_id, "
                "status, started_at, attempt_date) "
                "VALUES (:u, :a, :c, 'completed', now(), "
                "        (now() AT TIME ZONE 'Europe/Moscow')::date) "
                "RETURNING id"
            ),
            {"u": user_id, "a": activity_id, "c": channel_id},
        ).scalar_one()

    with pytest.raises(IntegrityError) as exc_info:
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rewards "
                    "(attempt_id, reward_type, code, is_bearer, redeem_otp, "
                    " redeem_otp_expires_at, issued_at) "
                    "VALUES (:aid, 'promocode', 'TEST-OTP-01', true, '123456', "
                    "        now() + interval '5 min', now())"
                ),
                {"aid": attempt_id},
            )
    assert CHECK_BEARER in str(exc_info.value)
