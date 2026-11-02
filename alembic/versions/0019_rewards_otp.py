"""rewards.is_bearer + redeem_otp + redeem_otp_expires_at (фаза 10b-1, C-1)

Revision ID: 0019_rewards_otp
Revises: 0018_tg_fsm_state
Create Date: 2026-10-19

Закрывает блокер C-1 из аудита (риск №3 из 09): именные награды (`voucher`,
`physical_gift`) гасятся по OTP, а не по коду. Текст канона уже с v0.2 ждёт этих
колонок: 02 §7, 03 §1 (rewards), 04 §1.2, 09 §3.

Схема (03 §1):
* `is_bearer BOOLEAN NOT NULL` — true для предъявительских (`promocode`),
  false для именных (`voucher`, `physical_gift`, `wheel_prize`, `leaflet`,
  `recommendation_set`). По таблице выставляем по текущему `reward_type`:
  `promocode` → true, остальное → false. Код просит OTP только у `voucher`
  и `physical_gift` (ядро проверяет), остальные `is_bearer=false` живут без OTP.
* `redeem_otp TEXT NULL` — одноразовый код погашения (6 цифр, см.
  `app/core/rewards/otp.py`). Создаётся по нажатию «подойду сейчас» в боте;
  погашение сбрасывает в NULL, чтобы unique index не держал погашенные коды.
* `redeem_otp_expires_at TIMESTAMPTZ NULL` — срок жизни OTP (5 мин, канон 02 §7).

Инварианты:
* CHECK `rewards_bearer_no_otp`: `(NOT is_bearer) OR (redeem_otp IS NULL)` —
  у предъявительских OTP быть не может. Канон 03 §1 (rewards): «CHECK: `(is_bearer
  AND redeem_otp IS NULL) OR (NOT is_bearer)`» — эквивалентно нашему условию.
* CHECK `rewards_otp_pair`: `(redeem_otp IS NULL) = (redeem_otp_expires_at IS NULL)` —
  оба поля всегда пусты вместе; иначе грязное состояние «код есть, срока нет».
* partial unique `rewards_active_otp` ON (redeem_otp) WHERE redeem_otp IS NOT NULL
  AND redeemed_at IS NULL — защита от совпадения двух живых OTP (канон 03 §1).

downgrade: симметрично (drop index → drop checks → drop columns).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_rewards_otp"
down_revision: str | None = "0018_tg_fsm_state"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 1. Nullable + default, чтобы существующие строки получили корректное значение.
    op.add_column(
        "rewards",
        sa.Column("is_bearer", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "rewards",
        sa.Column("redeem_otp", sa.Text(), nullable=True),
    )
    op.add_column(
        "rewards",
        sa.Column("redeem_otp_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # 2. Заполнить is_bearer по текущему типу: только promocode предъявительский.
    op.execute(
        "UPDATE rewards SET is_bearer = (reward_type = 'promocode') "
        "WHERE is_bearer IS NULL"
    )
    # 3. NOT NULL + server_default=false. SqlRewardRepo.insert всегда передаёт
    #    is_bearer явно (promocode → true, остальное → false); server_default нужен,
    #    чтобы внешние/тестовые вставки без указания колонки получали корректный
    #    инвариант (CHECK `rewards_bearer_no_otp` при `is_bearer=false` проходит).
    #    Postgres не разрешает DEFAULT с ссылкой на другую колонку (reward_type),
    #    поэтому дефолт — скаляр `false`; реальное бизнес-правило живёт в коде.
    op.alter_column(
        "rewards",
        "is_bearer",
        nullable=False,
        server_default=sa.text("false"),
    )

    # 4. CHECK-инварианты.
    op.create_check_constraint(
        "rewards_bearer_no_otp",
        "rewards",
        "(NOT is_bearer) OR (redeem_otp IS NULL)",
    )
    op.create_check_constraint(
        "rewards_otp_pair",
        "rewards",
        "(redeem_otp IS NULL) = (redeem_otp_expires_at IS NULL)",
    )

    # 5. partial unique: два живых OTP не могут совпасть; погашенные (redeem_otp=NULL)
    #    и просроченные (redeem_otp по-прежнему заполнен) — отдельная забота ядра,
    #    unique защищает только от коллизий активных кодов.
    op.create_index(
        "rewards_active_otp",
        "rewards",
        ["redeem_otp"],
        unique=True,
        postgresql_where=sa.text("redeem_otp IS NOT NULL AND redeemed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("rewards_active_otp", table_name="rewards")
    op.drop_constraint("rewards_otp_pair", "rewards", type_="check")
    op.drop_constraint("rewards_bearer_no_otp", "rewards", type_="check")
    op.drop_column("rewards", "redeem_otp_expires_at")
    op.drop_column("rewards", "redeem_otp")
    op.drop_column("rewards", "is_bearer")
