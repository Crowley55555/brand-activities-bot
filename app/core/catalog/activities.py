from __future__ import annotations

from app.common.errors import NotFound
from app.core.catalog.repository import CatalogRepo
from app.core.types.dto import ActivityRef
from app.core.types.ids import ActivityId


async def get_activity_by_slug(repo: CatalogRepo, slug: str) -> ActivityRef:
    """Активность, которую можно запустить. Нет / выключена / удалена -> NotFound."""
    activity = await repo.find_activity_by_slug(slug)
    if activity is None:
        raise NotFound("activity_not_found")
    return activity


async def get_activity_by_id(repo: CatalogRepo, activity_id: ActivityId) -> ActivityRef:
    """Активность по id без фильтра is_active: начатое прохождение можно закончить."""
    activity = await repo.find_activity_by_id(activity_id)
    if activity is None:
        raise NotFound("activity_not_found")
    return activity


async def list_activities_by_brand(repo: CatalogRepo, brand_slug: str) -> list[ActivityRef]:
    return await repo.list_activities_by_brand(brand_slug)
