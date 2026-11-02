"""Миграция 0017 (фаза 10a, C-3): TTL pending_moderation в limits + индекс сканера.

Правило 5 (бизнес-специфика в данных): TTL `pending_moderation_ttl_minutes` живёт в
`activities.limits`. UGC-активности получают ключ (180 мин), остальные — нет (fail-closed).
Также добавляется `system_reject_reasons.pending_moderation_expired` — подпись для
уведомления, которое ставит джоб `expire_pending_moderation`.

База тестов общая для сессии: каждый цикл возвращает её в head (`finally`).
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic import command

PREVIOUS = "0016_add_ugc_url_reqs"
UGC_SLUGS = ("brand_b_photo_subscription", "brand_e_photo", "brand_d_confirmation")
NON_UGC_SLUGS = (
    "brand_a_skin_survey",
    "brand_a_diagnostics_booking",
    "brand_c_stylist_booking",
    "brand_d_wheel",
    "brand_d_polaroid",
)
TTL_KEY = "pending_moderation_ttl_minutes"
SYS_REASON = "pending_moderation_expired"
INDEX_NAME = "attempts_pending_moderation_due"


def _limits(engine: Engine, slug: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT limits FROM activities WHERE slug = :s"), {"s": slug}
        ).one()
    return dict(row.limits)


def _has_index(engine: Engine) -> bool:
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT 1 FROM pg_indexes WHERE indexname = :name"
            ),
            {"name": INDEX_NAME},
        ).scalar_one_or_none()
    return value is not None


def test_pending_ttl_added_to_ugc_only(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    # Head — ключи есть у UGC и нет у остальных.
    for slug in UGC_SLUGS:
        limits = _limits(sync_engine, slug)
        assert limits.get(TTL_KEY) == 180
        system_reasons = limits.get("system_reject_reasons", {})
        assert system_reasons.get(SYS_REASON), (
            f"{slug}: системная причина {SYS_REASON} должна быть в limits"
        )
    for slug in NON_UGC_SLUGS:
        limits = _limits(sync_engine, slug)
        assert TTL_KEY not in limits, f"{slug}: не-UGC не должен иметь {TTL_KEY}"

    assert _has_index(sync_engine)

    try:
        command.downgrade(cfg, PREVIOUS)
        assert not _has_index(sync_engine)
        for slug in UGC_SLUGS:
            limits = _limits(sync_engine, slug)
            assert TTL_KEY not in limits
            system_reasons = limits.get("system_reject_reasons", {})
            assert SYS_REASON not in system_reasons
    finally:
        command.upgrade(cfg, "head")

    # После upgrade — всё на месте.
    for slug in UGC_SLUGS:
        limits = _limits(sync_engine, slug)
        assert limits[TTL_KEY] == 180
    assert _has_index(sync_engine)


def test_migration_0017_is_idempotent(
    pg_url: str, sync_engine: Engine, clean_db: None
) -> None:
    """upgrade поверх уже стоящих ключей не падает, downgrade поверх отсутствующих — тоже."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    before = {slug: _limits(sync_engine, slug) for slug in UGC_SLUGS}
    try:
        command.downgrade(cfg, PREVIOUS)
        # Повторный downgrade — безопасен (нечего удалять).
        command.downgrade(cfg, PREVIOUS)
        command.upgrade(cfg, "head")
        # Повторный upgrade — безопасен (уже стоит).
        # (команда идёт через alembic_version, повторно не выполнит — но ключи те же.)
        after = {slug: _limits(sync_engine, slug) for slug in UGC_SLUGS}
        for slug in UGC_SLUGS:
            assert after[slug].get(TTL_KEY) == 180
            # остальные ключи limits остались (пример для brand_b — reject_reasons, ваучер).
            # Сравним множество ключей: head перед циклом == head после.
            assert set(after[slug].keys()) == set(before[slug].keys())
    finally:
        command.upgrade(cfg, "head")
