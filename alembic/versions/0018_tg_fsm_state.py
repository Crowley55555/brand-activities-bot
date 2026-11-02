"""tg_fsm_state (фаза 10a, C-4)

Revision ID: 0018_tg_fsm_state
Revises: 0017_pending_moderation_ttl
Create Date: 2026-10-18

Персистентный FSM-storage для aiogram в `tg_worker` (C-4 ревью pre-pilot).
Канон: tg_worker ходит в ядро только по HTTP под HMAC (правило 4). Таблица
`tg_fsm_state` живёт в основной БД; доступ к ней — через `/internal/v1/channels/tg/fsm`.

Схема:
* `key TEXT PRIMARY KEY` — ключ `aiogram` в формате
  `<bot_id>:<chat_id>:<user_id>:<thread_id>:<business_connection_id>:<destiny>`.
  Собирается на стороне tg_worker (`fsm_storage.py`), сюда приходит как строка.
* `state TEXT NULL` — текущее состояние FSM (например `register_fsm:wait_phone`).
  NULL — состояние не задано (стартовый экран).
* `data JSONB NOT NULL DEFAULT '{}'` — пользовательские данные FSM (имя, фамилия и т. п.).
  В пределах tg_worker это транзитные значения регистрации; никаких внешних систем здесь нет.
* `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` — для будущей чистки протухших
  записей (отдельный джоб вне scope фазы 10a).

ПДн внутри `data` живут только до окончания регистрации: обработчик `/register`
подтверждает регистрацию в ядре и сразу вызывает `clear` на storage. Если гость
бросает диалог — поле `updated_at` помогает джобу-чистильщику удалить строку
(не входит в 10a; TODO в журнале 03). Правило 9 (ПДн не наружу): FSM-данные
не покидают периметр нашего сервиса — ни в Telegram, ни в S3.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_tg_fsm_state"
down_revision: str | None = "0017_pending_moderation_ttl"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tg_fsm_state",
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column(
            "data",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "tg_fsm_state_updated_at", "tg_fsm_state", ["updated_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("tg_fsm_state_updated_at", table_name="tg_fsm_state")
    op.drop_table("tg_fsm_state")
