"""Схема Бренд D (фаза 5): подарок за пост без файла, Polaroid-приёмка, код призов колеса.

ADR-42 / U11: ugc_file_iff_ingested разрешает строку без файла при post_url IS NOT NULL в статусах
pending/approved/rejected (подарок за пост). B1: frame_submissions - source_* nullable, статусы
receiving и ingest_failed (сбой загрузки из Telegram, файла нет). B3: wheel_prizes.code_template -
шаблон кода для призов prize_kind='promocode'. Данные (активности и призы) - в 0014.

Downgrade возвращает прежние CHECK. Он не пройдёт, если остались строки без файла или в статусе
receiving: на проде не откатываем (тесты чистят данные перед downgrade).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_brand_d_schema"
down_revision: str | None = "0012_seed_booking_activities"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_UGC_FILE_OLD = (
    "(status IN ('receiving','ingest_failed')) OR "
    "(file_bucket IS NOT NULL AND file_key IS NOT NULL "
    "AND file_content_type IS NOT NULL AND file_size_bytes IS NOT NULL)"
)
_UGC_FILE_NEW = (
    "(status IN ('receiving','ingest_failed') AND file_bucket IS NULL AND file_key IS NULL "
    "AND file_content_type IS NULL AND file_size_bytes IS NULL) "
    "OR (file_bucket IS NOT NULL AND file_key IS NOT NULL "
    "AND file_content_type IS NOT NULL AND file_size_bytes IS NOT NULL) "
    "OR (status IN ('pending','approved','rejected') AND post_url IS NOT NULL "
    "AND file_bucket IS NULL AND file_key IS NULL "
    "AND file_content_type IS NULL AND file_size_bytes IS NULL)"
)
_FRAME_STATUS_OLD = "status IN ('queued','done','failed')"
_FRAME_STATUS_NEW = "status IN ('receiving','queued','done','failed','ingest_failed')"
_FRAME_SIZE_OLD = "source_size_bytes > 0"
_FRAME_SIZE_NEW = "source_size_bytes IS NULL OR source_size_bytes > 0"
_FRAME_SOURCE_NEW = (
    "(status IN ('receiving','ingest_failed') AND source_bucket IS NULL AND source_key IS NULL "
    "AND source_content_type IS NULL AND source_size_bytes IS NULL) "
    "OR (status NOT IN ('receiving','ingest_failed') AND source_bucket IS NOT NULL "
    "AND source_key IS NOT NULL AND source_content_type IS NOT NULL "
    "AND source_size_bytes IS NOT NULL)"
)
_FRAME_FAILURE_OLD = "(status = 'failed') OR failure_reason IS NULL"
_FRAME_FAILURE_NEW = "(status IN ('failed','ingest_failed')) OR failure_reason IS NULL"
# Шаблон кода - только у призов promocode. Обязательность шаблона у promocode проверяет ядро при
# выдаче (grant_spec_invalid): односторонний CHECK не даёт откату и повторному upgrade терять данные.
_PRIZE_CODE_CHECK = "code_template IS NULL OR prize_kind = 'promocode'"
_FRAME_SOURCE_COLUMNS = (
    "source_bucket",
    "source_key",
    "source_content_type",
    "source_size_bytes",
)


def upgrade() -> None:
    # 1. UGC: строка подарка за пост без файла (ADR-42, U11).
    op.drop_constraint("ugc_file_iff_ingested", "ugc_submissions", type_="check")
    op.create_check_constraint("ugc_file_iff_ingested", "ugc_submissions", _UGC_FILE_NEW)

    # 2. Polaroid: приёмка receiving без исходника, сбой загрузки ingest_failed (B1).
    for column in _FRAME_SOURCE_COLUMNS:
        op.alter_column(
            "frame_submissions",
            column,
            existing_type=sa.BigInteger() if column == "source_size_bytes" else sa.Text(),
            nullable=True,
        )
    op.drop_constraint("frame_status", "frame_submissions", type_="check")
    op.create_check_constraint("frame_status", "frame_submissions", _FRAME_STATUS_NEW)
    op.drop_constraint("frame_source_size_pos", "frame_submissions", type_="check")
    op.create_check_constraint("frame_source_size_pos", "frame_submissions", _FRAME_SIZE_NEW)
    op.create_check_constraint("frame_source_iff_fetched", "frame_submissions", _FRAME_SOURCE_NEW)
    op.drop_constraint("frame_failure_only_when_failed", "frame_submissions", type_="check")
    op.create_check_constraint(
        "frame_failure_only_when_failed", "frame_submissions", _FRAME_FAILURE_NEW
    )

    # 3. Колесо: шаблон кода промокода только у призов prize_kind='promocode' (B3).
    op.add_column("wheel_prizes", sa.Column("code_template", sa.Text(), nullable=True))
    op.create_check_constraint("wheel_prizes_code_template", "wheel_prizes", _PRIZE_CODE_CHECK)


def downgrade() -> None:
    # Строки новых состояний не переживают откат: приёмка Polaroid без исходника и подарок за пост
    # без файла не проходят прежние CHECK и NOT NULL. Откат - для dev и тестов, на проде не запускаем.
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM frame_submissions WHERE source_bucket IS NULL"))
    bind.execute(sa.text("DELETE FROM ugc_submissions WHERE file_bucket IS NULL"))

    op.drop_constraint("wheel_prizes_code_template", "wheel_prizes", type_="check")
    op.drop_column("wheel_prizes", "code_template")

    op.drop_constraint("frame_failure_only_when_failed", "frame_submissions", type_="check")
    op.create_check_constraint(
        "frame_failure_only_when_failed", "frame_submissions", _FRAME_FAILURE_OLD
    )
    op.drop_constraint("frame_source_iff_fetched", "frame_submissions", type_="check")
    op.drop_constraint("frame_source_size_pos", "frame_submissions", type_="check")
    op.create_check_constraint(
        "frame_source_size_pos", "frame_submissions", _FRAME_SIZE_OLD
    )
    op.drop_constraint("frame_status", "frame_submissions", type_="check")
    op.create_check_constraint("frame_status", "frame_submissions", _FRAME_STATUS_OLD)
    for column in _FRAME_SOURCE_COLUMNS:
        op.alter_column(
            "frame_submissions",
            column,
            existing_type=sa.BigInteger() if column == "source_size_bytes" else sa.Text(),
            nullable=False,
        )

    op.drop_constraint("ugc_file_iff_ingested", "ugc_submissions", type_="check")
    op.create_check_constraint("ugc_file_iff_ingested", "ugc_submissions", _UGC_FILE_OLD)
