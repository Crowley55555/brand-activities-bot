from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.catalog.repository import CatalogRepo
from app.core.types.dto import ProductRecommendationRef
from app.core.types.ids import ActivityId


def condition_matches(condition: Mapping[str, str], profile: Mapping[str, str]) -> bool:
    """Карточка подходит, если все пары её условия есть в профиле гостя.

    Пустое условие подходит любому профилю («карточка для всех»).
    """
    return all(profile.get(attribute) == value for attribute, value in condition.items())


async def get_recommendations_by_condition(
    repo: CatalogRepo, activity_id: ActivityId, condition: Mapping[str, str]
) -> list[ProductRecommendationRef]:
    """Карточки активности, подходящие под профиль `condition`.

    Порядок: чем специфичнее условие (больше пар), тем раньше; при равенстве - по
    `position`, затем по id. Ограничение количества - забота вызывающего
    (настройка активности), здесь возвращаем всё подходящее.
    """
    cards = await repo.list_recommendations(activity_id)
    matched = [card for card in cards if condition_matches(card.condition, condition)]
    matched.sort(key=lambda card: (-len(card.condition), card.position, card.id))
    return matched


async def get_recommendations_by_ids(
    repo: CatalogRepo, ids: Sequence[int]
) -> list[ProductRecommendationRef]:
    """Актуальные карточки по id, включая soft-deleted; порядок как в `ids`.

    ADR-14: награда хранит только id; текст и URL берутся из справочника при показе,
    а удалённая карточка остаётся в истории гостя.
    """
    if not ids:
        return []
    found: dict[int, ProductRecommendationRef] = {
        int(card.id): card for card in await repo.list_recommendations_by_ids(ids)
    }
    return [found[i] for i in ids if i in found]
