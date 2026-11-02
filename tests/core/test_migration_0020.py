"""Миграция 0020 (фаза 10b-2, I-1): CHECK audit_log.object_type расширен до 'jobs'.

До 0020: вставка `audit_log.object_type='jobs'` падала на `audit_log_object_type`.
После: `jobs` в whitelist (для `action='jobs_dead_revive'/'jobs_dead_bury'`),
остальные значения из списка — на месте.

Downgrade симметричный: whitelist возвращается к списку из 0001.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from alembic import command

PREVIOUS = "0019_rewards_otp"


def _insert_audit(engine: Engine, object_type: str) -> int:
    """Вставить фиктивный аудит через SQL; создаёт админа, если его ещё нет."""
    with engine.begin() as conn:
        admin_id = conn.execute(
            text(
                "INSERT INTO admin_users (login, password_hash, display_name) "
                "VALUES (:login, 'test-hash', 'Audit') "
                "ON CONFLICT (login) DO UPDATE SET display_name = EXCLUDED.display_name "
                "RETURNING id"
            ),
            {"login": "audit-mig-0020"},
        ).scalar_one()
        audit_id = conn.execute(
            text(
                "INSERT INTO audit_log (actor_admin_id, action, object_type, "
                "  object_id, created_at) "
                "VALUES (:aid, 'test_action', :otype, 1, now()) RETURNING id"
            ),
            {"aid": int(admin_id), "otype": object_type},
        ).scalar_one()
    return int(audit_id)


def test_audit_log_accepts_jobs_object_type(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """После 0020: INSERT с object_type='jobs' проходит CHECK."""
    audit_id = _insert_audit(sync_engine, "jobs")
    assert audit_id > 0
    # Прочие валидные типы по-прежнему проходят.
    assert _insert_audit(sync_engine, "users") > 0


def test_audit_log_rejects_unknown_object_type(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """CHECK не превращается в no-op: посторонний object_type всё ещё отвергается."""
    with pytest.raises(IntegrityError):
        _insert_audit(sync_engine, "cats")


def test_migration_0020_reversible(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """downgrade возвращает CHECK к 0019-состоянию: 'jobs' снова отвергается."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    try:
        command.downgrade(cfg, PREVIOUS)
        with pytest.raises(IntegrityError):
            _insert_audit(sync_engine, "jobs")
        # Остальные типы по-прежнему валидны.
        assert _insert_audit(sync_engine, "users") > 0
    finally:
        command.upgrade(cfg, "head")

    # После upgrade — снова принимает jobs.
    assert _insert_audit(sync_engine, "jobs") > 0
