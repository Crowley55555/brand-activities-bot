from __future__ import annotations

from app.core.catalog import list_brands
from app.core.types.dto import BrandRef
from app.core.types.ids import BrandId


class FakeCatalogRepo:
    def __init__(self, brands: list[BrandRef]) -> None:
        self._brands = brands

    async def list_active_brands(self) -> list[BrandRef]:
        return list(self._brands)


async def test_list_brands_returns_active_only() -> None:
    repo = FakeCatalogRepo(
        [
            BrandRef(id=BrandId(1), slug="brand_a", display_name="Бренд A", tagline="x"),
            BrandRef(id=BrandId(2), slug="brand_b", display_name="Бренд B", tagline="y"),
        ]
    )
    result = await list_brands(repo)
    assert [b.slug for b in result] == ["brand_a", "brand_b"]


async def test_list_brands_empty() -> None:
    repo = FakeCatalogRepo([])
    assert await list_brands(repo) == []
