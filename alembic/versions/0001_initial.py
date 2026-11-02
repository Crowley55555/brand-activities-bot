"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-12

Полная схема по architecture/schema.sql: все таблицы, индексы, FK, CHECK,
триггеры и функции. Код среза trim-vertical-slice использует только
users+channel_accounts, остальные таблицы остаются пустыми до своих фаз.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


TS = sa.TIMESTAMP(timezone=True)


def _ts(name: str, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, TS, nullable=True)
    return sa.Column(name, TS, nullable=False, server_default=sa.text("now()"))


def _set_updated_at(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_set_updated_at "
        f"BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # ---------- 1. core ----------
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("first_name", sa.Text, nullable=True),
        sa.Column("last_name", sa.Text, nullable=True),
        sa.Column("phone", sa.Text, nullable=True),
        sa.Column("email", sa.Text, nullable=True),
        sa.Column("registered_at", TS, nullable=True),
        sa.Column("last_seen_at", TS, nullable=True),
        sa.Column("anonymized_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "users_phone_unique",
        "users",
        ["phone"],
        unique=True,
        postgresql_where=sa.text("phone IS NOT NULL"),
    )
    op.create_index(
        "users_email_unique",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    _set_updated_at("users")

    op.create_table(
        "consents",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.Text, nullable=False),
        sa.Column("text_hash", sa.Text, nullable=False),
        sa.Column("accepted_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("channel_type", sa.Text, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "char_length(text_hash) = 64", name="consents_text_hash_len"
        ),
        sa.CheckConstraint(
            "channel_type IN ('telegram','max','whatsapp','web')",
            name="consents_channel_type",
        ),
    )
    op.create_index(
        "consents_active",
        "consents",
        ["user_id", "policy_version"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("consents_user", "consents", ["user_id"])
    _set_updated_at("consents")

    op.create_table(
        "channel_accounts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("external_username", sa.Text, nullable=True),
        sa.Column("linked_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("unlinked_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "channel_type IN ('telegram','max','whatsapp','web')",
            name="channel_accounts_channel_type",
        ),
    )
    op.create_index(
        "channel_accounts_lookup",
        "channel_accounts",
        ["channel_type", "external_id"],
        unique=True,
        postgresql_where=sa.text("unlinked_at IS NULL"),
    )
    op.create_index("channel_accounts_user", "channel_accounts", ["user_id"])
    _set_updated_at("channel_accounts")

    # ---------- 2. catalog ----------
    op.create_table(
        "brands",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("tagline", sa.Text, nullable=True),
        sa.Column(
            "is_active", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    _set_updated_at("brands")

    op.create_table(
        "activities",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "brand_id",
            sa.BigInteger,
            sa.ForeignKey("brands.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column(
            "is_active", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "limits",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "kind IN ('survey','booking','ugc','wheel','photo_frame','confirmation')",
            name="activities_kind",
        ),
    )
    op.create_index("activities_brand", "activities", ["brand_id"])
    _set_updated_at("activities")

    op.create_table(
        "survey_questions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "activity_id",
            sa.BigInteger,
            sa.ForeignKey("activities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("question_type", sa.Text, nullable=False),
        sa.Column("options", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "question_type IN ('single_choice','multi_choice','free_text')",
            name="survey_questions_type",
        ),
    )
    op.create_index(
        "survey_questions_activity", "survey_questions", ["activity_id", "position"]
    )
    _set_updated_at("survey_questions")

    op.create_table(
        "product_recommendations",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "activity_id",
            sa.BigInteger,
            sa.ForeignKey("activities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("condition", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("product_name", sa.Text, nullable=False),
        sa.Column("caption", sa.Text, nullable=True),
        sa.Column("product_url", sa.Text, nullable=False),
        sa.Column("position", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "is_active", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "product_recommendations_activity",
        "product_recommendations",
        ["activity_id"],
    )
    _set_updated_at("product_recommendations")

    op.create_table(
        "wheel_prizes",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "activity_id",
            sa.BigInteger,
            sa.ForeignKey("activities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("prize_kind", sa.Text, nullable=False),
        sa.Column("weight", sa.Integer, nullable=False),
        sa.Column("stock_left", sa.Integer, nullable=True),
        sa.Column(
            "is_active", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "prize_kind IN ('mini_gift','compliment','promocode')",
            name="wheel_prizes_kind",
        ),
        sa.CheckConstraint("weight > 0", name="wheel_prizes_weight_pos"),
        sa.CheckConstraint(
            "stock_left IS NULL OR stock_left >= 0", name="wheel_prizes_stock_nonneg"
        ),
    )
    op.create_index("wheel_prizes_activity", "wheel_prizes", ["activity_id"])
    _set_updated_at("wheel_prizes")

    # ---------- 3. scheduling ----------
    op.create_table(
        "slots",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "activity_id",
            sa.BigInteger,
            sa.ForeignKey("activities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("start_at", TS, nullable=False),
        sa.Column("duration_minutes", sa.SmallInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 1 AND 240", name="slots_duration_range"
        ),
        sa.CheckConstraint("kind IN ('bookable','break')", name="slots_kind"),
    )
    op.create_index(
        "slots_bookable_by_activity",
        "slots",
        ["activity_id", "start_at"],
        postgresql_where=sa.text("kind = 'bookable' AND deleted_at IS NULL"),
    )
    _set_updated_at("slots")

    # ---------- 4. attempts ----------
    op.create_table(
        "attempts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "activity_id",
            sa.BigInteger,
            sa.ForeignKey("activities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "channel_account_id",
            sa.BigInteger,
            sa.ForeignKey("channel_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("started_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("attempt_date", sa.Date, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('in_progress','pending_moderation','approved','rejected',"
            "'cancelled','completed','expired')",
            name="attempts_status",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="attempts_finished_after_started",
        ),
        sa.CheckConstraint(
            "attempt_date = (started_at AT TIME ZONE 'Europe/Moscow')::date",
            name="attempts_date_matches_started",
        ),
    )
    op.create_index(
        "attempts_once_per_day",
        "attempts",
        ["user_id", "activity_id", "attempt_date"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('in_progress','pending_moderation','approved','completed')"
        ),
    )
    op.create_index(
        "attempts_user_recent",
        "attempts",
        ["user_id", sa.text("started_at DESC")],
    )
    op.create_index("attempts_activity", "attempts", ["activity_id"])
    op.create_index("attempts_channel", "attempts", ["channel_account_id"])
    op.create_index("attempts_old", "attempts", ["started_at"])
    _set_updated_at("attempts")

    op.create_table(
        "survey_answers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "attempt_id",
            sa.BigInteger,
            sa.ForeignKey("attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_id",
            sa.BigInteger,
            sa.ForeignKey("survey_questions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "selected_option_keys", sa.dialects.postgresql.JSONB, nullable=True
        ),
        sa.Column("free_text", sa.Text, nullable=True),
        sa.Column(
            "answered_at", TS, nullable=False, server_default=sa.text("now()")
        ),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint(
            "attempt_id", "question_id", name="survey_answers_attempt_question"
        ),
    )
    op.create_index("survey_answers_question", "survey_answers", ["question_id"])
    _set_updated_at("survey_answers")

    op.create_table(
        "appointments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "attempt_id",
            sa.BigInteger,
            sa.ForeignKey("attempts.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "slot_id",
            sa.BigInteger,
            sa.ForeignKey("slots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("remind_at", TS, nullable=False),
        sa.Column("auto_cancel_at", TS, nullable=False),
        sa.Column("reminded_at", TS, nullable=True),
        sa.Column("arrived_at", TS, nullable=True),
        sa.Column("arrived_marked_by_admin_id", sa.BigInteger, nullable=True),
        sa.Column("cancelled_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('booked','reminded','arrived','late',"
            "'cancelled_by_user','cancelled_auto')",
            name="appointments_status",
        ),
        sa.CheckConstraint(
            "auto_cancel_at > remind_at", name="appointments_cancel_after_remind"
        ),
    )
    op.create_index(
        "appointments_slot_active",
        "appointments",
        ["slot_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('booked','reminded','arrived')"
        ),
    )
    op.create_index(
        "appointments_remind_due",
        "appointments",
        ["remind_at"],
        postgresql_where=sa.text(
            "status = 'booked' AND reminded_at IS NULL"
        ),
    )
    op.create_index(
        "appointments_cancel_due",
        "appointments",
        ["auto_cancel_at"],
        postgresql_where=sa.text(
            "status IN ('booked','reminded') AND arrived_at IS NULL"
        ),
    )
    _set_updated_at("appointments")

    op.create_table(
        "ugc_submissions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "attempt_id",
            sa.BigInteger,
            sa.ForeignKey("attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_bucket", sa.Text, nullable=False),
        sa.Column("file_key", sa.Text, nullable=False),
        sa.Column("file_content_type", sa.Text, nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=False),
        sa.Column("post_url", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("rejection_reason_slug", sa.Text, nullable=True),
        sa.Column("rejection_note", sa.Text, nullable=True),
        sa.Column("decided_by_admin_id", sa.BigInteger, nullable=True),
        sa.Column(
            "submitted_at", TS, nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("decided_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("file_size_bytes > 0", name="ugc_size_pos"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected')", name="ugc_status"
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)",
            name="ugc_pending_iff_decided_null",
        ),
        sa.CheckConstraint(
            "(status = 'rejected') OR "
            "(rejection_reason_slug IS NULL AND rejection_note IS NULL)",
            name="ugc_rejection_only_when_rejected",
        ),
    )
    op.create_index("ugc_submissions_attempt", "ugc_submissions", ["attempt_id"])
    op.create_index(
        "ugc_pending",
        "ugc_submissions",
        ["submitted_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    _set_updated_at("ugc_submissions")

    op.create_table(
        "wheel_spins",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "attempt_id",
            sa.BigInteger,
            sa.ForeignKey("attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wheel_prize_id",
            sa.BigInteger,
            sa.ForeignKey("wheel_prizes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "activity_id",
            sa.BigInteger,
            sa.ForeignKey("activities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("spun_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("spin_date", sa.Date, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "spin_date = (spun_at AT TIME ZONE 'Europe/Moscow')::date",
            name="wheel_spins_date_matches",
        ),
    )
    op.create_index(
        "wheel_spins_once_per_day",
        "wheel_spins",
        ["user_id", "activity_id", "spin_date"],
        unique=True,
    )
    op.create_index("wheel_spins_prize", "wheel_spins", ["wheel_prize_id"])
    op.create_index("wheel_spins_attempt", "wheel_spins", ["attempt_id"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION wheel_spins_validate_denorm()
        RETURNS trigger AS $$
        DECLARE
            a_user_id BIGINT;
            a_activity_id BIGINT;
        BEGIN
            SELECT user_id, activity_id INTO a_user_id, a_activity_id
            FROM attempts WHERE id = NEW.attempt_id;
            IF a_user_id IS DISTINCT FROM NEW.user_id
                OR a_activity_id IS DISTINCT FROM NEW.activity_id THEN
                RAISE EXCEPTION 'wheel_spins denorm mismatch with attempt %',
                    NEW.attempt_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER wheel_spins_check_denorm
            BEFORE INSERT OR UPDATE ON wheel_spins
            FOR EACH ROW EXECUTE FUNCTION wheel_spins_validate_denorm();
        """
    )
    _set_updated_at("wheel_spins")

    op.create_table(
        "frame_submissions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "attempt_id",
            sa.BigInteger,
            sa.ForeignKey("attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_bucket", sa.Text, nullable=False),
        sa.Column("source_key", sa.Text, nullable=False),
        sa.Column("source_content_type", sa.Text, nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger, nullable=False),
        sa.Column("result_bucket", sa.Text, nullable=True),
        sa.Column("result_key", sa.Text, nullable=True),
        sa.Column("result_content_type", sa.Text, nullable=True),
        sa.Column("result_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("completed_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("source_size_bytes > 0", name="frame_source_size_pos"),
        sa.CheckConstraint(
            "result_size_bytes IS NULL OR result_size_bytes > 0",
            name="frame_result_size_pos",
        ),
        sa.CheckConstraint(
            "status IN ('queued','done','failed')", name="frame_status"
        ),
        sa.CheckConstraint(
            "(status = 'done') = (result_key IS NOT NULL)",
            name="frame_done_iff_result",
        ),
        sa.CheckConstraint(
            "(status = 'failed') OR failure_reason IS NULL",
            name="frame_failure_only_when_failed",
        ),
    )
    op.create_index("frame_submissions_attempt", "frame_submissions", ["attempt_id"])
    op.create_index(
        "frame_submissions_queued",
        "frame_submissions",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    _set_updated_at("frame_submissions")

    # ---------- 5. rewards ----------
    op.create_table(
        "rewards",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "attempt_id",
            sa.BigInteger,
            sa.ForeignKey("attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reward_type", sa.Text, nullable=False),
        sa.Column("code", sa.Text, nullable=True),
        sa.Column(
            "product_recommendation_ids",
            sa.dialects.postgresql.JSONB,
            nullable=True,
        ),
        sa.Column(
            "wheel_prize_id",
            sa.BigInteger,
            sa.ForeignKey("wheel_prizes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("issued_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("redeemed_at", TS, nullable=True),
        sa.Column("redeemed_by_admin_id", sa.BigInteger, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("revoke_reason", sa.Text, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "reward_type IN ('promocode','voucher','physical_gift',"
            "'recommendation_set','leaflet','wheel_prize')",
            name="rewards_type",
        ),
        sa.CheckConstraint(
            "redeemed_at IS NULL OR redeemed_at >= issued_at",
            name="rewards_redeemed_after_issued",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="rewards_revoked_after_issued",
        ),
        sa.CheckConstraint(
            "(reward_type IN ('promocode','voucher') AND code IS NOT NULL) "
            "OR (reward_type NOT IN ('promocode','voucher') AND code IS NULL)",
            name="rewards_code_matches_type",
        ),
        sa.CheckConstraint(
            "(reward_type = 'recommendation_set' "
            "AND product_recommendation_ids IS NOT NULL) "
            "OR (reward_type <> 'recommendation_set' "
            "AND product_recommendation_ids IS NULL)",
            name="rewards_recs_matches_type",
        ),
        sa.CheckConstraint(
            "(reward_type = 'wheel_prize' AND wheel_prize_id IS NOT NULL) "
            "OR (reward_type <> 'wheel_prize' AND wheel_prize_id IS NULL)",
            name="rewards_wheel_matches_type",
        ),
    )
    op.create_index(
        "rewards_per_attempt_type",
        "rewards",
        ["attempt_id", "reward_type"],
        unique=True,
    )
    op.create_index(
        "rewards_code",
        "rewards",
        ["code"],
        unique=True,
        postgresql_where=sa.text("code IS NOT NULL"),
    )
    op.create_index("rewards_attempt", "rewards", ["attempt_id"])
    op.create_index(
        "rewards_active_codes",
        "rewards",
        ["reward_type", sa.text("issued_at DESC")],
        postgresql_where=sa.text("revoked_at IS NULL AND code IS NOT NULL"),
    )
    _set_updated_at("rewards")

    # ---------- 6. admin ----------
    op.create_table(
        "admin_users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("login", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column(
            "is_active", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column("last_login_at", TS, nullable=True),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    _set_updated_at("admin_users")

    op.create_foreign_key(
        "appointments_arrived_by_fkey",
        "appointments",
        "admin_users",
        ["arrived_marked_by_admin_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "ugc_submissions_decided_by_fkey",
        "ugc_submissions",
        "admin_users",
        ["decided_by_admin_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "rewards_redeemed_by_fkey",
        "rewards",
        "admin_users",
        ["redeemed_by_admin_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "admin_brands",
        sa.Column(
            "admin_id",
            sa.BigInteger,
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "brand_id",
            sa.BigInteger,
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_at", TS, nullable=False, server_default=sa.text("now()")
        ),
        sa.PrimaryKeyConstraint("admin_id", "brand_id"),
    )
    op.create_index("admin_brands_brand", "admin_brands", ["brand_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "actor_admin_id",
            sa.BigInteger,
            sa.ForeignKey("admin_users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("object_type", sa.Text, nullable=False),
        sa.Column("object_id", sa.BigInteger, nullable=False),
        sa.Column(
            "occurred_at", TS, nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("details", sa.dialects.postgresql.JSONB, nullable=True),
        _ts("created_at"),
        sa.CheckConstraint(
            "(actor_admin_id IS NOT NULL) <> (actor_user_id IS NOT NULL)",
            name="audit_log_actor_xor",
        ),
        sa.CheckConstraint(
            "object_type IN ("
            "'users','consents','channel_accounts','attempts','rewards',"
            "'ugc_submissions','appointments','wheel_spins','frame_submissions'"
            ")",
            name="audit_log_object_type",
        ),
    )
    op.create_index(
        "audit_log_time", "audit_log", [sa.text("occurred_at DESC")]
    )
    op.create_index(
        "audit_log_object", "audit_log", ["object_type", "object_id"]
    )
    op.create_index(
        "audit_log_actor_admin",
        "audit_log",
        ["actor_admin_id"],
        postgresql_where=sa.text("actor_admin_id IS NOT NULL"),
    )
    op.create_index(
        "audit_log_actor_user",
        "audit_log",
        ["actor_user_id"],
        postgresql_where=sa.text("actor_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log CASCADE;")
    op.execute("DROP TABLE IF EXISTS admin_brands CASCADE;")
    op.execute("DROP TABLE IF EXISTS admin_users CASCADE;")
    op.execute("DROP TABLE IF EXISTS rewards CASCADE;")
    op.execute("DROP TABLE IF EXISTS frame_submissions CASCADE;")
    op.execute("DROP TABLE IF EXISTS wheel_spins CASCADE;")
    op.execute("DROP TABLE IF EXISTS ugc_submissions CASCADE;")
    op.execute("DROP TABLE IF EXISTS appointments CASCADE;")
    op.execute("DROP TABLE IF EXISTS survey_answers CASCADE;")
    op.execute("DROP TABLE IF EXISTS attempts CASCADE;")
    op.execute("DROP TABLE IF EXISTS slots CASCADE;")
    op.execute("DROP TABLE IF EXISTS wheel_prizes CASCADE;")
    op.execute("DROP TABLE IF EXISTS product_recommendations CASCADE;")
    op.execute("DROP TABLE IF EXISTS survey_questions CASCADE;")
    op.execute("DROP TABLE IF EXISTS activities CASCADE;")
    op.execute("DROP TABLE IF EXISTS brands CASCADE;")
    op.execute("DROP TABLE IF EXISTS channel_accounts CASCADE;")
    op.execute("DROP TABLE IF EXISTS consents CASCADE;")
    op.execute("DROP TABLE IF EXISTS users CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS wheel_spins_validate_denorm() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE;")
