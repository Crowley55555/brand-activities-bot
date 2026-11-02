"""Миграция 0011 (ТОП-1 ревью фазы 3c): системные причины Бренд B в activities.limits.

Проверяем содержимое после upgrade head, затем downgrade -1 и повторный upgrade head (up/down/up).
База тестов общая для сессии, поэтому цикл возвращает её в исходное состояние.
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic import command

SYSTEM_KEYS = {
    "mime_mismatch",
    "file_not_found",
    "file_too_large",
    "ingest_dead",
    "consent_revoked",
    # Миграция 0017 (фаза 10a) дописала системную причину для джоба
    # expire_pending_moderation.
    "pending_moderation_expired",
}


def _system_keys(engine: Engine) -> set[str] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT limits -> 'system_reject_reasons' AS reasons FROM activities "
                "WHERE slug = 'brand_b_photo_subscription'"
            )
        ).one()
    if row.reasons is None:
        return None
    return set(row.reasons.keys())


def test_migration_0011_up_down_up(pg_url: str, sync_engine: Engine, clean_db: None) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    assert _system_keys(sync_engine) == SYSTEM_KEYS

    command.downgrade(cfg, "0010_seed_brand_b_reject_reasons")
    assert _system_keys(sync_engine) is None

    command.upgrade(cfg, "head")
    assert _system_keys(sync_engine) == SYSTEM_KEYS
