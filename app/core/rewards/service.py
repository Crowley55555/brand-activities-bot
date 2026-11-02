from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Final

from app.common.errors import ConflictError, NotFound, ValidationError
from app.core.catalog import CatalogRepo, get_recommendations_by_ids
from app.core.rewards.code_generator import RandomSource, generate_code
from app.core.rewards.repository import RewardRepo
from app.core.types.dto import GrantSpec, RewardIssued
from app.core.types.enums import RewardType
from app.core.types.ids import AttemptId, RewardId, UserId

# ADR-13: до пяти попыток на коллизию глобально уникального rewards.code.
MAX_CODE_ATTEMPTS: Final[int] = 5

_CODE_TYPES: Final[frozenset[RewardType]] = frozenset({"promocode", "voucher"})


def _validate_spec(spec: GrantSpec) -> None:
    """Форма GrantSpec должна совпадать с CHECK-ограничениями таблицы rewards.

    Ловим здесь, чтобы вместо IntegrityError из базы получить понятный ValidationError.
    """
    needs_code = spec.reward_type in _CODE_TYPES
    needs_recs = spec.reward_type == "recommendation_set"
    needs_wheel = spec.reward_type == "wheel_prize"
    has_recs = spec.product_recommendation_ids is not None
    valid = (
        (spec.code_template is not None) == needs_code
        and has_recs == needs_recs
        and (spec.wheel_prize_id is not None) == needs_wheel
        and (not needs_code or bool(spec.code_template))
        and (not needs_recs or bool(spec.product_recommendation_ids))
    )
    if not valid:
        raise ValidationError("grant_spec_invalid")


async def _grant_one(
    repo: RewardRepo,
    attempt_id: AttemptId,
    spec: GrantSpec,
    now: datetime,
    random_source: RandomSource | None,
) -> RewardIssued:
    recommendation_ids = (
        list(spec.product_recommendation_ids)
        if spec.product_recommendation_ids is not None
        else None
    )
    if spec.code_template is None:
        reward = await repo.insert(
            attempt_id,
            spec.reward_type,
            None,
            recommendation_ids,
            spec.wheel_prize_id,
            spec.title,
            now,
        )
        if reward is None:
            raise ConflictError("duplicate_grant")
        return reward

    for _ in range(MAX_CODE_ATTEMPTS):
        code = generate_code(spec.code_template, random_source)
        reward = await repo.insert(
            attempt_id,
            spec.reward_type,
            code,
            recommendation_ids,
            spec.wheel_prize_id,
            spec.title,
            now,
        )
        if reward is not None:
            return reward
        # Конфликт: либо награда этого типа уже есть (дубль выдачи), либо код занят.
        if await repo.exists(attempt_id, spec.reward_type):
            raise ConflictError("duplicate_grant")
    raise ConflictError("code_collision")


async def grant_on_completion(
    repo: RewardRepo,
    attempt_id: AttemptId,
    grants: Sequence[GrantSpec],
    now: datetime,
    random_source: RandomSource | None = None,
) -> list[RewardIssued]:
    """Выдать награды за завершённое прохождение (08 §4.1, 04 §4.4).

    Вызывается из той же транзакции, что `close_attempt`: любая ошибка (дубль, исчерпанный
    ретрай кода) откатывает всё, прохождение не остаётся completed без наград.
    Анти-дубль держит БД: UNIQUE (attempt_id, reward_type) -> ConflictError('duplicate_grant').
    """
    if not grants:
        raise ValidationError("grant_spec_invalid")
    for spec in grants:
        _validate_spec(spec)
    issued: list[RewardIssued] = []
    for spec in grants:
        issued.append(await _grant_one(repo, attempt_id, spec, now, random_source))
    return issued


async def revoke(repo: RewardRepo, reward_id: RewardId, reason: str, now: datetime) -> None:
    """Отозвать награду (soft: `revoked_at`, строка остаётся для аудита)."""
    if not reason.strip():
        raise ValidationError("revoke_reason_required")
    if await repo.revoke(reward_id, reason, now):
        return
    if await repo.get(reward_id) is None:
        raise NotFound("reward_not_found")
    raise ConflictError("already_revoked")


async def list_by_user(
    repo: RewardRepo, catalog: CatalogRepo, user_id: UserId
) -> list[RewardIssued]:
    """История наград гостя; для recommendation_set - актуальные карточки (ADR-14).

    В награде лежат только id. Название и URL берутся из справочника сейчас: правка
    карточки видна в истории, а мягко удалённая или выключенная карточка остаётся
    (иначе гость потерял бы часть своего набора).
    """
    rewards = await repo.list_by_user(user_id)
    wanted: list[int] = []
    for reward in rewards:
        for card_id in reward.product_recommendation_ids or ():
            if card_id not in wanted:
                wanted.append(card_id)
    cards = {int(card.id): card for card in await get_recommendations_by_ids(catalog, wanted)}
    return [
        replace(
            reward,
            recommendations=tuple(
                cards[card_id]
                for card_id in reward.product_recommendation_ids or ()
                if card_id in cards
            ),
        )
        for reward in rewards
    ]
