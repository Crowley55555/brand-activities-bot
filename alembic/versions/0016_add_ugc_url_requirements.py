"""add ugc url requirements (фаза 6.5)

Revision ID: 0016_add_ugc_url_reqs
Revises: 0015_seed_brand_e
Create Date: 2026-10-21

Флаги ссылок для UGC-активностей в `activities.limits` (known-limit B-1). Правило 5: что просить у
гостя кроме фото, решают данные, а не код tg_worker. Диалог в Telegram читает два ключа:
* `requires_post_url` - после фото просить ссылку на пост;
* `requires_subscription_url` - просить ссылку на профиль подписки.

Значения:
* `brand_b_photo_subscription` - true / true (фото, пост и подписка уходят на модерацию);
* `brand_e_photo` - false / false (только фото).

Миграция только дописывает два ключа через `jsonb_set`; остальные ключи limits не меняются.
Повтор безопасен: `jsonb_set` перезаписывает ключ. `downgrade` убирает оба ключа оператором `-` и
безопасен, если ключей уже нет. Схема не меняется.

Имя ревизии короче 32 символов: alembic хранит её в alembic_version.version_num (VARCHAR(32)).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_add_ugc_url_reqs"
down_revision: str | None = "0015_seed_brand_e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# (slug, requires_post_url, requires_subscription_url)
_FLAGS: list[tuple[str, bool, bool]] = [
    ("brand_b_photo_subscription", True, True),
    ("brand_e_photo", False, False),
]

_SET_FLAGS = sa.text(
    "UPDATE activities SET limits = jsonb_set("
    "jsonb_set(limits, '{requires_post_url}', to_jsonb(CAST(:post AS BOOLEAN))), "
    "'{requires_subscription_url}', to_jsonb(CAST(:subscription AS BOOLEAN))), "
    "updated_at = now() "
    "WHERE slug = :slug"
)

_DROP_FLAGS = sa.text(
    "UPDATE activities SET limits = limits - 'requires_post_url' - 'requires_subscription_url', "
    "updated_at = now() "
    "WHERE slug = :slug"
)


def upgrade() -> None:
    bind = op.get_bind()
    for slug, post, subscription in _FLAGS:
        bind.execute(_SET_FLAGS, {"slug": slug, "post": post, "subscription": subscription})


def downgrade() -> None:
    bind = op.get_bind()
    for slug, _post, _subscription in _FLAGS:
        bind.execute(_DROP_FLAGS, {"slug": slug})
