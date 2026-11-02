"""Миграция 0016 (фаза 6.5): флаги requires_post_url / requires_subscription_url в limits UGC.

Правило 5: что просить у гостя кроме фото (ссылку на пост, ссылку на подписку), решают данные
`activities.limits`, а не код tg_worker. Миграция только дописывает два ключа; остальные ключи
limits остаются как были (сравниваем состояние до и после, а не только сами флаги).

База тестов общая для сессии, поэтому каждый цикл возвращает её в head (`finally`). Откат 0016
трогает только `limits`: внешних ключей на эти строки он не задевает.
"""

from __future__ import annotations

import json
from typing import Any

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic import command

PREVIOUS = "0015_seed_brand_e"
WITH_LINKS_SLUG = "brand_b_photo_subscription"
PHOTO_ONLY_SLUG = "brand_e_photo"
SLUGS = (WITH_LINKS_SLUG, PHOTO_ONLY_SLUG)
POST_FLAG = "requires_post_url"
SUBSCRIPTION_FLAG = "requires_subscription_url"
FLAGS = (POST_FLAG, SUBSCRIPTION_FLAG)
# Миграция 0017 (фаза 10a) добавила ключи, которых не было в 0015 — учитываем в
# сравнении состояний до/после downgrade.
TTL_KEY = "pending_moderation_ttl_minutes"
SYS_REASON = "pending_moderation_expired"


def _limits(engine: Engine) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT slug, limits FROM activities WHERE slug IN (:with_links, :photo_only)"),
            {"with_links": WITH_LINKS_SLUG, "photo_only": PHOTO_ONLY_SLUG},
        ).all()
    return {str(row.slug): dict(row.limits) for row in rows}


def _without_phase_10a(limits: dict[str, Any]) -> dict[str, Any]:
    """Срезаем ключи, добавленные миграцией 0017 (TTL + системная причина в словаре)."""
    cleaned = {key: value for key, value in limits.items() if key != TTL_KEY}
    reasons = cleaned.get("system_reject_reasons")
    if isinstance(reasons, dict) and SYS_REASON in reasons:
        cleaned = dict(cleaned)
        cleaned["system_reject_reasons"] = {
            k: v for k, v in reasons.items() if k != SYS_REASON
        }
    return cleaned


def _without_flags(limits: dict[str, Any]) -> dict[str, Any]:
    """Срезаем флаги 0016 и ключи 0017 (так как тест проверяет состояние после 0015)."""
    base = {key: value for key, value in limits.items() if key not in FLAGS}
    return _without_phase_10a(base)


def _overwrite_flags(engine: Engine, slug: str, *, post: bool, subscription: bool) -> None:
    """Ставит флаги вручную (как будто их уже записали раньше с другими значениями)."""
    patch = json.dumps({POST_FLAG: post, SUBSCRIPTION_FLAG: subscription})
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE activities SET limits = limits || CAST(:patch AS jsonb) "
                "WHERE slug = :slug"
            ),
            {"patch": patch, "slug": slug},
        )


def _drop_flags(engine: Engine, slug: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE activities SET limits = limits - 'requires_post_url' "
                "- 'requires_subscription_url' WHERE slug = :slug"
            ),
            {"slug": slug},
        )


def test_migration_0016_up_down_up(pg_url: str, sync_engine: Engine, clean_db: None) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)

    at_head = _limits(sync_engine)
    assert set(at_head) == set(SLUGS)
    # Фото с постом и подпиской просит обе ссылки, фото без ссылок - ни одной.
    assert at_head[WITH_LINKS_SLUG][POST_FLAG] is True
    assert at_head[WITH_LINKS_SLUG][SUBSCRIPTION_FLAG] is True
    assert at_head[PHOTO_ONLY_SLUG][POST_FLAG] is False
    assert at_head[PHOTO_ONLY_SLUG][SUBSCRIPTION_FLAG] is False

    try:
        command.downgrade(cfg, PREVIOUS)
        previous = _limits(sync_engine)
        for slug in SLUGS:
            assert not set(FLAGS) & set(previous[slug])
            # Миграция добавила ровно два ключа: всё остальное в limits осталось прежним.
            assert previous[slug] == _without_flags(at_head[slug])
    finally:
        # База тестов общая для сессии: при сбое отката возвращаем схему и сиды в head.
        command.upgrade(cfg, "head")
    assert _limits(sync_engine) == at_head


def test_migration_0016_is_idempotent(pg_url: str, sync_engine: Engine, clean_db: None) -> None:
    """upgrade перезаписывает уже стоящие флаги, downgrade не падает на отсутствующих."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    at_head = _limits(sync_engine)

    try:
        # У активности без ссылок ключей уже нет: откат проходит и ничего лишнего не трогает.
        _drop_flags(sync_engine, PHOTO_ONLY_SLUG)
        command.downgrade(cfg, PREVIOUS)
        for slug in SLUGS:
            assert not set(FLAGS) & set(_limits(sync_engine)[slug])

        # Флаги стоят, но с неверными значениями: upgrade ставит нужные, чужие ключи целы.
        _overwrite_flags(sync_engine, WITH_LINKS_SLUG, post=False, subscription=False)
        _overwrite_flags(sync_engine, PHOTO_ONLY_SLUG, post=True, subscription=True)
        command.upgrade(cfg, "head")
        assert _limits(sync_engine) == at_head
    finally:
        command.upgrade(cfg, "head")
