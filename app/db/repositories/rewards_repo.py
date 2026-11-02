from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rewards.otp import OtpCardRow, RewardOtpEligibility
from app.core.rewards.repository import ActiveNamedReward, RewardStatusFilter
from app.core.types.dto import RewardIssued, RewardListRow
from app.core.types.enums import RewardType
from app.core.types.ids import AdminId, AttemptId, RewardId, UserId

_COLUMNS = (
    "id, attempt_id, reward_type, code, title, issued_at, "
    "product_recommendation_ids, expires_at, redeemed_at, revoked_at"
)

_FILTER_SQL = (
    "FROM rewards r "
    "JOIN attempts a ON a.id = r.attempt_id "
    "JOIN activities act ON act.id = a.activity_id "
    "JOIN brands b ON b.id = act.brand_id "
    "WHERE r.reward_type = ANY(CAST(:types AS text[])) "
    "AND (:status = 'all' "
    "     OR (:status = 'issued' AND r.redeemed_at IS NULL AND r.revoked_at IS NULL) "
    "     OR (:status = 'redeemed' AND r.redeemed_at IS NOT NULL) "
    "     OR (:status = 'revoked' AND r.revoked_at IS NOT NULL)) "
    "AND (CAST(:slug AS text) IS NULL OR act.slug = :slug) "
)


def _to_issued(row: Any) -> RewardIssued:
    raw_ids = row.product_recommendation_ids
    return RewardIssued(
        id=RewardId(row.id),
        attempt_id=AttemptId(row.attempt_id),
        reward_type=cast(RewardType, row.reward_type),
        code=row.code,
        title=row.title,
        issued_at=row.issued_at,
        product_recommendation_ids=(
            tuple(int(i) for i in raw_ids) if raw_ids is not None else None
        ),
        expires_at=row.expires_at,
        redeemed_at=row.redeemed_at,
        revoked_at=row.revoked_at,
    )


class SqlRewardRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        attempt_id: AttemptId,
        reward_type: RewardType,
        code: str | None,
        product_recommendation_ids: Sequence[int] | None,
        wheel_prize_id: int | None,
        title: str | None,
        issued_at: datetime,
    ) -> RewardIssued | None:
        # ON CONFLICT без цели ловит оба уникальных индекса: rewards_per_attempt_type и
        # rewards_code. Исключения нет, транзакция жива - можно повторять с новым кодом.
        # 0019: is_bearer выставляем по типу: только promocode предъявительский (03 §1).
        is_bearer = reward_type == "promocode"
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO rewards "
                    "(attempt_id, reward_type, code, product_recommendation_ids, "
                    " wheel_prize_id, title, issued_at, is_bearer, "
                    " created_at, updated_at) "
                    "VALUES (:aid, :rtype, :code, CAST(:recs AS jsonb), "
                    " :wpid, :title, :ts, :bearer, :ts, :ts) "
                    "ON CONFLICT DO NOTHING "
                    f"RETURNING {_COLUMNS}"
                ),
                {
                    "aid": int(attempt_id),
                    "rtype": reward_type,
                    "code": code,
                    "recs": (
                        json.dumps([int(i) for i in product_recommendation_ids])
                        if product_recommendation_ids is not None
                        else None
                    ),
                    "wpid": wheel_prize_id,
                    "title": title,
                    "ts": issued_at,
                    "bearer": is_bearer,
                },
            )
        ).one_or_none()
        return _to_issued(row) if row is not None else None

    async def exists(self, attempt_id: AttemptId, reward_type: RewardType) -> bool:
        row = (
            await self._session.execute(
                text(
                    "SELECT 1 FROM rewards WHERE attempt_id = :aid AND reward_type = :rtype LIMIT 1"
                ),
                {"aid": int(attempt_id), "rtype": reward_type},
            )
        ).first()
        return row is not None

    async def get(self, reward_id: RewardId) -> RewardIssued | None:
        row = (
            await self._session.execute(
                text(f"SELECT {_COLUMNS} FROM rewards WHERE id = :id"),
                {"id": int(reward_id)},
            )
        ).one_or_none()
        return _to_issued(row) if row is not None else None

    async def revoke(self, reward_id: RewardId, reason: str, revoked_at: datetime) -> bool:
        row = (
            await self._session.execute(
                text(
                    "UPDATE rewards SET revoked_at = GREATEST(:ts, issued_at), "
                    "revoke_reason = :reason "
                    "WHERE id = :id AND revoked_at IS NULL RETURNING id"
                ),
                {"ts": revoked_at, "reason": reason, "id": int(reward_id)},
            )
        ).first()
        return row is not None

    async def list_for_attempt(self, attempt_id: AttemptId) -> list[RewardIssued]:
        rows = (
            await self._session.execute(
                text(f"SELECT {_COLUMNS} FROM rewards WHERE attempt_id = :aid ORDER BY id"),
                {"aid": int(attempt_id)},
            )
        ).all()
        return [_to_issued(row) for row in rows]

    async def list_by_ids(self, reward_ids: Sequence[int]) -> list[RewardIssued]:
        rows = (
            await self._session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM rewards "
                    "WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
                ),
                {"ids": [int(item) for item in reward_ids]},
            )
        ).all()
        return [_to_issued(row) for row in rows]

    async def list_by_user(self, user_id: UserId) -> list[RewardIssued]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT r.id, r.attempt_id, r.reward_type, r.code, r.title, "
                    "r.issued_at, r.product_recommendation_ids, r.expires_at, "
                    "r.redeemed_at, r.revoked_at "
                    "FROM rewards r JOIN attempts a ON a.id = r.attempt_id "
                    "WHERE a.user_id = :uid "
                    "ORDER BY r.issued_at DESC, r.id DESC"
                ),
                {"uid": int(user_id)},
            )
        ).all()
        return [_to_issued(row) for row in rows]

    async def list_filtered(
        self,
        reward_types: Sequence[RewardType],
        status: RewardStatusFilter,
        activity_slug: str | None,
        limit: int,
        offset: int,
    ) -> list[RewardListRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT r.id, r.issued_at, r.reward_type, r.code, r.title, r.attempt_id, "
                    "a.user_id, act.slug AS activity_slug, "
                    "act.display_name AS activity_display_name, b.slug AS brand_slug, "
                    "r.redeemed_at, r.revoked_at "
                    + _FILTER_SQL
                    + "ORDER BY r.issued_at DESC, r.id DESC LIMIT :limit OFFSET :offset"
                ),
                {
                    "types": list(reward_types),
                    "status": status,
                    "slug": activity_slug,
                    "limit": limit,
                    "offset": offset,
                },
            )
        ).all()
        return [
            RewardListRow(
                id=int(row.id),
                issued_at=row.issued_at,
                reward_type=row.reward_type,
                code=row.code,
                title=row.title,
                attempt_id=int(row.attempt_id),
                user_id=int(row.user_id),
                activity_slug=row.activity_slug,
                activity_display_name=row.activity_display_name,
                brand_slug=row.brand_slug,
                redeemed_at=row.redeemed_at,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]

    async def count_filtered(
        self,
        reward_types: Sequence[RewardType],
        status: RewardStatusFilter,
        activity_slug: str | None,
    ) -> int:
        value = (
            await self._session.execute(
                text("SELECT count(*) " + _FILTER_SQL),
                {"types": list(reward_types), "status": status, "slug": activity_slug},
            )
        ).scalar_one()
        return int(value)

    # ----- OTP-погашение (фаза 10b-1) -----

    async def get_otp_eligibility(
        self, reward_id: RewardId, user_id: UserId
    ) -> RewardOtpEligibility | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT r.reward_type, r.is_bearer, r.redeemed_at, r.revoked_at "
                    "FROM rewards r JOIN attempts a ON a.id = r.attempt_id "
                    "WHERE r.id = :rid AND a.user_id = :uid"
                ),
                {"rid": int(reward_id), "uid": int(user_id)},
            )
        ).one_or_none()
        if row is None:
            return None
        return RewardOtpEligibility(
            reward_type=cast(RewardType, row.reward_type),
            is_bearer=bool(row.is_bearer),
            redeemed_at=row.redeemed_at,
            revoked_at=row.revoked_at,
        )

    async def set_otp(
        self, reward_id: RewardId, otp: str, expires_at: datetime
    ) -> bool:
        # fix-1 I-1 (10b-1): атомарная проверка «нет живого OTP с тем же значением»
        # прямо в UPDATE через `AND NOT EXISTS`. Симметрично с `insert` (ON CONFLICT
        # DO NOTHING RETURNING) и `redeem_by_otp` (один атомарный UPDATE): коллизия
        # partial unique `rewards_active_otp` не возбуждает IntegrityError, UPDATE
        # просто не затронет строк. Core-цикл повторяет попытку с новым OTP.
        row = (
            await self._session.execute(
                text(
                    "UPDATE rewards SET redeem_otp = :otp, "
                    "redeem_otp_expires_at = :exp "
                    "WHERE id = :rid "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM rewards other "
                    "    WHERE other.redeem_otp = :otp "
                    "      AND other.redeemed_at IS NULL "
                    "      AND other.id <> :rid"
                    "  ) "
                    "RETURNING id"
                ),
                {"otp": otp, "exp": expires_at, "rid": int(reward_id)},
            )
        ).first()
        return row is not None

    async def find_card_by_otp(
        self, otp: str, now: datetime
    ) -> OtpCardRow | None:
        # JOIN attempts -> users -> channel_accounts(telegram). Берём ник из активной
        # привязки Telegram (unlinked_at IS NULL): админу у стойки она и нужна.
        row = (
            await self._session.execute(
                text(
                    "SELECT r.id AS reward_id, r.reward_type, r.title, r.code, "
                    "       u.id AS user_id, u.first_name, u.last_name, "
                    "       u.anonymized_at, "
                    "       ca.external_username AS telegram_username "
                    "FROM rewards r "
                    "JOIN attempts a ON a.id = r.attempt_id "
                    "JOIN users u ON u.id = a.user_id "
                    "LEFT JOIN channel_accounts ca "
                    "       ON ca.user_id = u.id "
                    "       AND ca.channel_type = 'telegram' "
                    "       AND ca.unlinked_at IS NULL "
                    "WHERE r.redeem_otp = :otp "
                    "  AND r.redeem_otp_expires_at > :now "
                    "  AND r.redeemed_at IS NULL "
                    "LIMIT 1"
                ),
                {"otp": otp, "now": now},
            )
        ).one_or_none()
        if row is None:
            return None
        return OtpCardRow(
            reward_id=RewardId(int(row.reward_id)),
            reward_type=cast(RewardType, row.reward_type),
            title=row.title,
            code=row.code,
            user_id=UserId(int(row.user_id)),
            first_name=row.first_name,
            last_name=row.last_name,
            telegram_username=row.telegram_username,
            anonymized_at=row.anonymized_at,
        )

    async def redeem_by_otp(
        self, otp: str, admin_id: AdminId, now: datetime
    ) -> RewardId | None:
        row = (
            await self._session.execute(
                text(
                    "UPDATE rewards SET "
                    "  redeemed_at = :now, "
                    "  redeemed_by_admin_id = :aid, "
                    "  redeem_otp = NULL, "
                    "  redeem_otp_expires_at = NULL "
                    "WHERE redeem_otp = :otp "
                    "  AND redeem_otp_expires_at > :now "
                    "  AND redeemed_at IS NULL "
                    "  AND is_bearer = false "
                    "RETURNING id"
                ),
                {"otp": otp, "aid": int(admin_id), "now": now},
            )
        ).one_or_none()
        if row is None:
            return None
        return RewardId(int(row.id))

    async def redeem_by_otp_and_id(
        self, otp: str, reward_id: RewardId, admin_id: AdminId, now: datetime
    ) -> bool:
        # fix-1 I-2 (10b-1): тот же атомарный UPDATE, но дополнительно `AND id = :rid`.
        # Это защита от UX-подделки {reward_id} в URL admin-формы: админ открыл
        # карточку гостя A, набрал OTP гостя B, нажал «Погасил» — наше ядро не
        # должно гасить чужую награду B под маской карточки A. 0 строк затронуто ⇒
        # core бросит `ConflictError('otp_mismatch')`.
        row = (
            await self._session.execute(
                text(
                    "UPDATE rewards SET "
                    "  redeemed_at = :now, "
                    "  redeemed_by_admin_id = :aid, "
                    "  redeem_otp = NULL, "
                    "  redeem_otp_expires_at = NULL "
                    "WHERE id = :rid "
                    "  AND redeem_otp = :otp "
                    "  AND redeem_otp_expires_at > :now "
                    "  AND redeemed_at IS NULL "
                    "  AND is_bearer = false "
                    "RETURNING id"
                ),
                {
                    "otp": otp,
                    "rid": int(reward_id),
                    "aid": int(admin_id),
                    "now": now,
                },
            )
        ).one_or_none()
        return row is not None

    async def list_active_named_rewards_by_user(
        self, user_id: UserId
    ) -> list[ActiveNamedReward]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT r.id, r.reward_type, r.title, r.code, r.issued_at "
                    "FROM rewards r JOIN attempts a ON a.id = r.attempt_id "
                    "WHERE a.user_id = :uid "
                    "  AND r.reward_type IN ('voucher', 'physical_gift') "
                    "  AND r.redeemed_at IS NULL "
                    "  AND r.revoked_at IS NULL "
                    "ORDER BY r.issued_at DESC, r.id DESC"
                ),
                {"uid": int(user_id)},
            )
        ).all()
        return [
            ActiveNamedReward(
                reward_id=RewardId(int(row.id)),
                reward_type=cast(RewardType, row.reward_type),
                title=row.title,
                code=row.code,
                issued_at=row.issued_at,
            )
            for row in rows
        ]
