"""pending moderation ttl (фаза 10a, C-3)

Revision ID: 0017_pending_moderation_ttl
Revises: 0016_add_ugc_url_reqs
Create Date: 2026-10-18

TTL для `pending_moderation` в `activities.limits.pending_moderation_ttl_minutes`
(C-3 ревью pre-pilot). Канон 03 §382 (ADR-20): модератор должен принять решение;
без TTL брошенная подача держит прохождение до конца дня — гость не может повторить.

Добавляем ключ только тем активностям, которые реально проходят через
`pending_moderation`:

* `brand_b_photo_subscription` (kind=`ugc`)
* `brand_e_photo` (kind=`ugc`)
* `brand_d_confirmation` (kind=`confirmation`, UGC-«подарок за пост»)

Значение — 180 минут (3 часа): модератор на стенде закроет быстрее, а брошенные
подачи после ивента ядро аккуратно отменит. Не UGC-активностям (booking, survey,
wheel, photo_frame) ключ не добавляем — у них нет `pending_moderation`, fail-closed.

Параллельно расширяем словарь `system_reject_reasons`: слаг
`pending_moderation_expired` -> подпись «Мы не успели принять решение по вашей
работе» (правило 5: подпись в БД, не в коде). Эту подпись подставит
notify_ingest_failed, который ставит сам джоб `expire_pending_moderation`.

Миграция только дописывает ключи в `limits` (jsonb_set / `||`); схема не меняется.
Повтор безопасен. `downgrade` снимает оба ключа (`-`), безопасен если ключей нет.

Индекс `attempts_pending_moderation_due` создаётся для сканера джоба (быстрый выбор
просроченных по `updated_at`). CONCURRENTLY не используем: alembic оборачивает
миграцию в транзакцию, DO BLOCK с commit не стыкуется с нашим стилем остальных
миграций (0001..0016 все обычные). Таблица attempts у нас небольшая до пилота.

Имя ревизии 29 символов — укладывается в VARCHAR(32) (alembic_version.version_num).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_pending_moderation_ttl"
down_revision: str | None = "0016_add_ugc_url_reqs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_UGC_SLUGS: tuple[str, ...] = (
    "brand_b_photo_subscription",
    "brand_e_photo",
    "brand_d_confirmation",
)
_TTL_MINUTES = 180
_SYSTEM_REASON_SLUG = "pending_moderation_expired"
_SYSTEM_REASON_LABEL = (
    "Мы не успели принять решение по вашей работе. Попробуйте ещё раз в следующий раз."
)
# Полный ключ system_reject_reasons существует у ugc-активностей после миграции 0011
# (brand_b) и 0014/0015 (brand_d_confirmation и brand_e_photo). Дополняем словарь ключом
# pending_moderation_expired: `||` сливает JSONB поверх существующего.
_PATCH_TTL = sa.text(
    "UPDATE activities SET limits = jsonb_set(limits, "
    "'{pending_moderation_ttl_minutes}', to_jsonb(CAST(:mins AS INTEGER))), "
    "updated_at = now() WHERE slug = :slug"
)
_PATCH_REASON = sa.text(
    "UPDATE activities SET limits = jsonb_set(limits, "
    "'{system_reject_reasons}', "
    "COALESCE(limits->'system_reject_reasons', '{}'::jsonb) || CAST(:patch AS jsonb)), "
    "updated_at = now() WHERE slug = :slug"
)
_DROP_TTL = sa.text(
    "UPDATE activities SET limits = limits - 'pending_moderation_ttl_minutes', "
    "updated_at = now() WHERE slug = :slug"
)
_DROP_REASON = sa.text(
    "UPDATE activities SET limits = jsonb_set(limits, '{system_reject_reasons}', "
    "COALESCE(limits->'system_reject_reasons', '{}'::jsonb) - :reason), "
    "updated_at = now() WHERE slug = :slug "
    "AND limits ? 'system_reject_reasons'"
)


def upgrade() -> None:
    bind = op.get_bind()
    reason_patch = json.dumps(
        {_SYSTEM_REASON_SLUG: _SYSTEM_REASON_LABEL}, ensure_ascii=False
    )
    for slug in _UGC_SLUGS:
        bind.execute(_PATCH_TTL, {"slug": slug, "mins": _TTL_MINUTES})
        bind.execute(_PATCH_REASON, {"slug": slug, "patch": reason_patch})
    # Индекс для сканера: просроченные pending_moderation по updated_at.
    op.create_index(
        "attempts_pending_moderation_due",
        "attempts",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending_moderation'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index("attempts_pending_moderation_due", table_name="attempts")
    for slug in _UGC_SLUGS:
        bind.execute(_DROP_REASON, {"slug": slug, "reason": _SYSTEM_REASON_SLUG})
        bind.execute(_DROP_TTL, {"slug": slug})
