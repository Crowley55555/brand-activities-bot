"""audit_log.object_type ← 'jobs' (фаза 10b-2, I-1)

Revision ID: 0020_audit_log_jobs
Revises: 0019_rewards_otp
Create Date: 2026-10-19

Фаза 10b-2 добавляет DLQ-UI (`/admin/jobs/dead`): админ видит `jobs.status='dead'`
и либо повторяет их (`jobs_dead_revive`), либо хоронит (`jobs_dead_bury`). Каждая
кнопка пишет строку в `audit_log` (канон 07 §2.3). Текущая CHECK-ограничение
`audit_log_object_type` (миграция 0001, строки 809-815) не разрешает 'jobs':
расширяем список.

Расширение CHECK — пересоздание ограничения с новым списком. SQL-операция
дешёвая (нет сканирования таблицы при добавлении значения в whitelist),
`downgrade` возвращает старый список.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_audit_log_jobs"
down_revision: str | None = "0019_rewards_otp"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD = (
    "object_type IN ("
    "'users','consents','channel_accounts','attempts','rewards',"
    "'ugc_submissions','appointments','wheel_spins','frame_submissions'"
    ")"
)
_NEW = (
    "object_type IN ("
    "'users','consents','channel_accounts','attempts','rewards',"
    "'ugc_submissions','appointments','wheel_spins','frame_submissions',"
    "'jobs'"
    ")"
)


def upgrade() -> None:
    op.drop_constraint("audit_log_object_type", "audit_log", type_="check")
    op.create_check_constraint("audit_log_object_type", "audit_log", _NEW)


def downgrade() -> None:
    op.drop_constraint("audit_log_object_type", "audit_log", type_="check")
    op.create_check_constraint("audit_log_object_type", "audit_log", _OLD)
