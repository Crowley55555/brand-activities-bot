from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base

# rewards ссылается на attempts: регистрируем модель в общем MetaData.
from app.db.models.attempts import Attempt  # noqa: F401, I001


class Reward(Base):
    """Зеркало rewards из 0001_initial.py + 0019_rewards_otp.py (фаза 10b-1).

    Колонки `is_bearer`, `redeem_otp`, `redeem_otp_expires_at` добавлены миграцией
    0019 (OTP-погашение именных наград, канон 02 §7 / 03 §1 / 04 §1.2 / 09 §3).
    """

    __tablename__ = "rewards"
    __table_args__ = (
        CheckConstraint(
            "reward_type IN ('promocode','voucher','physical_gift',"
            "'recommendation_set','leaflet','wheel_prize')",
            name="rewards_type",
        ),
        CheckConstraint(
            "redeemed_at IS NULL OR redeemed_at >= issued_at",
            name="rewards_redeemed_after_issued",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="rewards_revoked_after_issued",
        ),
        CheckConstraint(
            "(reward_type IN ('promocode','voucher') AND code IS NOT NULL) "
            "OR (reward_type NOT IN ('promocode','voucher') AND code IS NULL)",
            name="rewards_code_matches_type",
        ),
        CheckConstraint(
            "(reward_type = 'recommendation_set' "
            "AND product_recommendation_ids IS NOT NULL) "
            "OR (reward_type <> 'recommendation_set' "
            "AND product_recommendation_ids IS NULL)",
            name="rewards_recs_matches_type",
        ),
        CheckConstraint(
            "(reward_type = 'wheel_prize' AND wheel_prize_id IS NOT NULL) "
            "OR (reward_type <> 'wheel_prize' AND wheel_prize_id IS NULL)",
            name="rewards_wheel_matches_type",
        ),
        # 0019: у предъявительских OTP быть не может (03 §1 CHECK).
        CheckConstraint(
            "(NOT is_bearer) OR (redeem_otp IS NULL)",
            name="rewards_bearer_no_otp",
        ),
        # 0019: оба OTP-поля заполняются и сбрасываются парой.
        CheckConstraint(
            "(redeem_otp IS NULL) = (redeem_otp_expires_at IS NULL)",
            name="rewards_otp_pair",
        ),
        # Анти-дубль выдачи: один тип награды на прохождение (ADR-13, 04 §4.4).
        Index("rewards_per_attempt_type", "attempt_id", "reward_type", unique=True),
        # Промокод/ваучер глобально уникален среди не-NULL.
        Index(
            "rewards_code",
            "code",
            unique=True,
            postgresql_where=text("code IS NOT NULL"),
        ),
        Index("rewards_attempt", "attempt_id"),
        # 0019: два живых OTP не могут совпасть; погашенные (redeem_otp=NULL) вне индекса.
        Index(
            "rewards_active_otp",
            "redeem_otp",
            unique=True,
            postgresql_where=text("redeem_otp IS NOT NULL AND redeemed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reward_type: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ADR-14: только id карточек на момент выдачи, не их содержимое.
    product_recommendation_ids: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    wheel_prize_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    redeemed_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0019 (фаза 10b-1): флаг предъявительской награды и OTP для именных.
    is_bearer: Mapped[bool] = mapped_column(Boolean, nullable=False)
    redeem_otp: Mapped[str | None] = mapped_column(Text, nullable=True)
    redeem_otp_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
