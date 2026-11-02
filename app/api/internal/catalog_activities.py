from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.api.schemas import ActivitiesListOut, ActivityOut
from app.core.catalog import list_activities_by_brand
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.catalog_activities")

router = APIRouter(prefix="/internal/v1/catalog")


@router.get("/brands/{brand_slug}/activities", response_model=ActivitiesListOut)
async def list_brand_activities_endpoint(
    brand_slug: str,
    _body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> ActivitiesListOut:
    """Активности бренда: tg_worker узнаёт, что запускать, не зная slug'ов заранее (правило 5).

    `limits` отдаются целиком (ADR-45): адаптер читает из них флаги диалога и в БД не ходит.
    """
    activities = await list_activities_by_brand(uow.repositories.catalog, brand_slug)
    return ActivitiesListOut(
        activities=[
            ActivityOut(
                slug=a.slug, display_name=a.display_name, kind=a.kind, limits=dict(a.limits)
            )
            for a in activities
        ]
    )
