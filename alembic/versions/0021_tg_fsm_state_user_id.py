"""tg_fsm_state.user_id для /forget каскада (фаза 10b-3, I-2)

Revision ID: 0021_tg_fsm_state_user_id
Revises: 0020_audit_log_jobs
Create Date: 2026-10-19

Фаза 10b-3 закрывает I-2 аудита pre-pilot: `/forget` каскад обязан удалить
`tg_fsm_state` этого пользователя (иначе ПДн из FSM регистрации висят до
джоба-чистильщика). Канон 08 §335 (`/forget` → анонимизация + каскад) +
правило 9 (ПДн не наружу): после подтверждения гостем «ПОДТВЕРЖДАЮ» строки
FSM-регистрации надо удалить физически.

Варианты (см. §3.3 промта 10b-3):
* удалять `WHERE key LIKE '<bot_id>:<chat_id>:<user_id>:%'` — ядро не знает
  `bot_id`, `chat_id` без обращения к `channel_accounts`; LIKE по PK-TEXT
  делает scan, не index-lookup;
* **выбран (ADR-48):** добавить колонку `user_id BIGINT NULL` с индексом;
  роутер `/internal/v1/channels/tg/fsm/*` принимает `user_id` опционально,
  `/forget` удаляет `WHERE user_id = :uid`. BIGINT NULL совместим с историей
  (строки до миграции — NULL, не трогаем их в каскаде, их удалит
  `cleanup_stale_fsm` джоб-чистильщик по `updated_at`).

Индекс `tg_fsm_state_user_id`:
* partial `WHERE user_id IS NOT NULL` — экономим размер, старые строки без
  user_id не индексируются (их чистит `cleanup_stale_fsm` по `updated_at`);
* non-unique — один пользователь может держать несколько ключей FSM
  (разные `destiny`/`thread_id`), а также исторические строки без user_id.

Downgrade: удаляет индекс и колонку. Исторических данных миграция не трогает.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_tg_fsm_state_user_id"
down_revision: str | None = "0020_audit_log_jobs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tg_fsm_state",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "tg_fsm_state_user_id",
        "tg_fsm_state",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("tg_fsm_state_user_id", table_name="tg_fsm_state")
    op.drop_column("tg_fsm_state", "user_id")
