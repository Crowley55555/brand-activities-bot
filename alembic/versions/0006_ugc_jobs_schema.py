"""ugc_submissions: приём в receiving (ADR-16) и таблица jobs (ADR-18)

Revision ID: 0006_ugc_jobs_schema
Revises: 0005_attempts_expires_at
Create Date: 2026-10-16

Фаза 3a Бренд B UGC. Одна DDL-ревизия (решение оркестратора, пп. 1-5):

* `ugc_submissions.status` получает `receiving` (принято, файл ещё не лёг в хранилище)
  и `ingest_failed` (загрузка не удалась, гость пришлёт фото снова), 04 §5.2.
* `file_bucket`, `file_key`, `file_content_type`, `file_size_bytes` становятся NULL-able:
  в `receiving` и `ingest_failed` файла в хранилище нет. CHECK `ugc_file_iff_ingested`
  требует файл для всех остальных статусов.
* CHECK `ugc_pending_iff_decided_null` заменён на `ugc_decided_iff_final`: решение
  (`decided_at`) есть только у `approved` и `rejected`.
* `subscription_claimed_url` (NULL) - ссылка на профиль подписки для Бренд B
  (поле UGCSubmission из внутреннего архитектурного документа). Тело ссылки на пост остаётся в `post_url`.
* Таблица `jobs` по 04 §3.1 с индексами `jobs_claim_batch_idx`, `jobs_by_idempotency`,
  `jobs_dead_recent` и `jobs_locked_expiry` (последний - по 04 §3.1, для джоба
  разблокировки в фазе 3c). В 0001-0005 и в schema.sql таблицы `jobs` не было.

Downgrade - только dev (03 §5.2). Переходные строки `receiving`/`ingest_failed` в старой
схеме не выражаются, поэтому удаляются до возврата NOT NULL и прежних CHECK.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006_ugc_jobs_schema"
down_revision: str | None = "0005_attempts_expires_at"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

TS = sa.TIMESTAMP(timezone=True)


def _ts(name: str) -> sa.Column:
    return sa.Column(name, TS, nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    # 1. Статусы приёмки UGC.
    op.drop_constraint("ugc_status", "ugc_submissions", type_="check")
    op.create_check_constraint(
        "ugc_status",
        "ugc_submissions",
        "status IN ('receiving','pending','approved','rejected','ingest_failed')",
    )

    # 2. Файл появляется позже приёма; без файла допустимы только переходные статусы.
    op.alter_column("ugc_submissions", "file_bucket", existing_type=sa.Text(), nullable=True)
    op.alter_column("ugc_submissions", "file_key", existing_type=sa.Text(), nullable=True)
    op.alter_column("ugc_submissions", "file_content_type", existing_type=sa.Text(), nullable=True)
    op.alter_column(
        "ugc_submissions", "file_size_bytes", existing_type=sa.BigInteger(), nullable=True
    )
    op.create_check_constraint(
        "ugc_file_iff_ingested",
        "ugc_submissions",
        "(status IN ('receiving','ingest_failed')) OR "
        "(file_bucket IS NOT NULL AND file_key IS NOT NULL "
        "AND file_content_type IS NOT NULL AND file_size_bytes IS NOT NULL)",
    )

    # 3. Решение (decided_at) только у финальных статусов.
    op.drop_constraint("ugc_pending_iff_decided_null", "ugc_submissions", type_="check")
    op.create_check_constraint(
        "ugc_decided_iff_final",
        "ugc_submissions",
        "(status IN ('approved','rejected')) = (decided_at IS NOT NULL)",
    )

    # 4. Ссылка на профиль подписки (Бренд B).
    op.add_column(
        "ugc_submissions",
        sa.Column("subscription_claimed_url", sa.Text(), nullable=True),
    )

    # 5. Очередь фоновых задач (04 §3.1).
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("6")),
        sa.Column("run_after", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("locked_by", sa.Text, nullable=True),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('queued','locked','done','failed','dead')", name="jobs_status"
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts > 0", name="jobs_attempts_range"),
    )
    op.create_index(
        "jobs_claim_batch_idx",
        "jobs",
        ["run_after"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "jobs_by_idempotency",
        "jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "jobs_dead_recent",
        "jobs",
        [sa.text("updated_at DESC")],
        postgresql_where=sa.text("status = 'dead'"),
    )
    op.create_index(
        "jobs_locked_expiry",
        "jobs",
        ["locked_until"],
        postgresql_where=sa.text("status = 'locked'"),
    )
    op.execute(
        "CREATE TRIGGER jobs_set_updated_at BEFORE UPDATE ON jobs "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )


def downgrade() -> None:
    op.drop_table("jobs")  # триггер и индексы уходят вместе с таблицей

    # Переходные строки не выражаются в схеме 0005: удаляем до возврата ограничений.
    op.execute("DELETE FROM ugc_submissions WHERE status IN ('receiving','ingest_failed')")

    op.drop_column("ugc_submissions", "subscription_claimed_url")

    op.drop_constraint("ugc_decided_iff_final", "ugc_submissions", type_="check")
    op.create_check_constraint(
        "ugc_pending_iff_decided_null",
        "ugc_submissions",
        "(status = 'pending') = (decided_at IS NULL)",
    )

    op.drop_constraint("ugc_file_iff_ingested", "ugc_submissions", type_="check")
    op.alter_column(
        "ugc_submissions", "file_size_bytes", existing_type=sa.BigInteger(), nullable=False
    )
    op.alter_column("ugc_submissions", "file_content_type", existing_type=sa.Text(), nullable=False)
    op.alter_column("ugc_submissions", "file_key", existing_type=sa.Text(), nullable=False)
    op.alter_column("ugc_submissions", "file_bucket", existing_type=sa.Text(), nullable=False)

    op.drop_constraint("ugc_status", "ugc_submissions", type_="check")
    op.create_check_constraint(
        "ugc_status",
        "ugc_submissions",
        "status IN ('pending','approved','rejected')",
    )
