"""Список наград для админки (фаза 7b): только коды, которые гость предъявляет на кассе."""

from __future__ import annotations

from typing import Final, cast

from app.common.errors import ValidationError
from app.core.rewards.repository import RewardRepo, RewardStatusFilter
from app.core.types.dto import RewardListRow
from app.core.types.enums import RewardType

_TYPE_FILTERS: Final[dict[str, tuple[RewardType, ...]]] = {
    "all": ("promocode", "voucher"),
    "promocode": ("promocode",),
    "voucher": ("voucher",),
}
_STATUSES: Final[frozenset[str]] = frozenset({"all", "issued", "redeemed", "revoked"})


def _resolve_types(reward_type: str) -> tuple[RewardType, ...]:
    try:
        return _TYPE_FILTERS[reward_type]
    except KeyError:
        raise ValidationError(f"unknown reward type filter: {reward_type}") from None


async def list_rewards_page(
    rewards: RewardRepo,
    reward_type: str,
    status: str,
    activity_slug: str | None,
    limit: int,
    offset: int,
) -> tuple[list[RewardListRow], int]:
    """Страница наград и общее число под фильтры; wheel_prize/leaflet/... не отдаются."""
    types = _resolve_types(reward_type)
    if status not in _STATUSES:
        raise ValidationError(f"unknown reward status filter: {status}")
    status_filter = cast(RewardStatusFilter, status)
    slug = activity_slug or None
    rows = await rewards.list_filtered(types, status_filter, slug, limit, offset)
    return rows, await rewards.count_filtered(types, status_filter, slug)
