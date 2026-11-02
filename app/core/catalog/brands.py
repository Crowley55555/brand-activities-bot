from __future__ import annotations

from app.core.catalog.repository import CatalogRepo
from app.core.types.dto import BrandRef


async def list_brands(repo: CatalogRepo) -> list[BrandRef]:
    """Активные бренды, отсортированы по display_name.

    Правило 5: названия и slug'и брендов — только в seed-миграции
    `alembic/versions/0002_seed_brands.py`, не в коде.
    """
    return await repo.list_active_brands()
