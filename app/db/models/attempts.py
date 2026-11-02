from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base

# attempts ссылается на users, activities, channel_accounts; survey_answers - на
# survey_questions: регистрируем эти модели в общем MetaData.
from app.db.models.catalog import Activity, SurveyQuestion  # noqa: F401, I001
from app.db.models.core import ChannelAccount, User  # noqa: F401, I001


class Attempt(Base):
    """Зеркало attempts из alembic/versions/0001_initial.py (главный агрегат)."""

    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress','pending_moderation','approved','rejected',"
            "'cancelled','completed','expired')",
            name="attempts_status",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="attempts_finished_after_started",
        ),
        CheckConstraint(
            "attempt_date = (started_at AT TIME ZONE 'Europe/Moscow')::date",
            name="attempts_date_matches_started",
        ),
        # «Один раз в день»: база сама не даст создать второе прохождение (03 §2 Q3).
        Index(
            "attempts_once_per_day",
            "user_id",
            "activity_id",
            "attempt_date",
            unique=True,
            postgresql_where=text(
                "status IN ('in_progress','pending_moderation','approved','completed',"
                "'rejected')"
            ),
        ),
        Index("attempts_activity", "activity_id"),
        Index("attempts_channel", "channel_account_id"),
        Index("attempts_old", "started_at"),
        # Джоб expire_stale_attempts (04 §1.3): скан только живых in_progress.
        Index(
            "attempts_expire_due",
            "expires_at",
            postgresql_where=text("status = 'in_progress'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    activity_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("activities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("channel_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    attempt_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class UgcSubmission(Base):
    """Зеркало ugc_submissions (0001 + 0006): фото и ссылки гостя, дочь прохождения.

    Файл лежит в объектном хранилище, в БД только его ссылка (03 §4). `file_*` пусты в
    `receiving` и `ingest_failed`; решение (`decided_at`) есть только у approved/rejected.
    """

    __tablename__ = "ugc_submissions"
    __table_args__ = (
        CheckConstraint("file_size_bytes > 0", name="ugc_size_pos"),
        CheckConstraint(
            "status IN ('receiving','pending','approved','rejected','ingest_failed')",
            name="ugc_status",
        ),
        CheckConstraint(
            "(status IN ('receiving','ingest_failed')) OR "
            "(file_bucket IS NOT NULL AND file_key IS NOT NULL "
            "AND file_content_type IS NOT NULL AND file_size_bytes IS NOT NULL)",
            name="ugc_file_iff_ingested",
        ),
        CheckConstraint(
            "(status IN ('approved','rejected')) = (decided_at IS NOT NULL)",
            name="ugc_decided_iff_final",
        ),
        CheckConstraint(
            "(status = 'rejected') OR "
            "(rejection_reason_slug IS NULL AND rejection_note IS NULL)",
            name="ugc_rejection_only_when_rejected",
        ),
        Index("ugc_submissions_attempt", "attempt_id"),
        # F1 (миграция 0009): одна активная подача на прохождение - защита от гонки.
        Index(
            "ugc_submission_active",
            "attempt_id",
            unique=True,
            postgresql_where=text("status IN ('receiving','pending','approved')"),
        ),
        Index(
            "ugc_pending",
            "submitted_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    subscription_claimed_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rejection_reason_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Без FK в зеркале: admin_users живёт в app/db/models/admin.py (как в rewards.py).
    decided_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class SurveyAnswer(Base):
    """Зеркало survey_answers: ответ гостя на вопрос (дочь прохождения)."""

    __tablename__ = "survey_answers"
    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="survey_answers_attempt_question"),
        Index("survey_answers_question", "question_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("survey_questions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    selected_option_keys: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
