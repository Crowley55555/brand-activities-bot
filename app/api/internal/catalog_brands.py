from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.api.schemas import BrandOut, BrandsListOut
from app.core.catalog import list_brands
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.catalog_brands")

router = APIRouter(prefix="/internal/v1/catalog")


@router.get("/brands", response_model=BrandsListOut)
async def list_brands_endpoint(
    _body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> BrandsListOut:
    brands = await list_brands(uow.repositories.catalog)
    return BrandsListOut(
        brands=[
            BrandOut(slug=b.slug, display_name=b.display_name, tagline=b.tagline)
            for b in brands
        ]
    )
