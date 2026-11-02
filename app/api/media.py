"""GET /media/{submission_id}?t=<token>: фото UGC по подписанной ссылке (П2 фазы 3b, 302 фазы 3c).

local_fs (dev): байты с кодом 200 из объектного хранилища. selectel_s3 (prod, ADR-29): 302 на
presigned_get, файл отдаёт само хранилище; контракт ссылки тот же. Маршрут вне /admin: сессия не
нужна, защита - подпись и срок (правило 9: бакет и ключ наружу не отдаём; Location - только
подписанный URL на TTL).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import RedirectResponse, Response

from app.api.deps import get_uow
from app.config import Settings, get_settings
from app.core.media import (
    resolve_bytes,
    resolve_frame_bytes,
    resolve_frame_ref,
    resolve_ref,
)
from app.core.types.ids import UgcSubmissionId
from app.db.unit_of_work import UnitOfWork

router = APIRouter()

# Фото не кешируем на диске у гостя или прокси и не даём браузеру угадывать тип (nosniff).
_MEDIA_HEADERS: dict[str, str] = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/media/{submission_id}", include_in_schema=False)
async def get_media(
    submission_id: int,
    request: Request,
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
    t: Annotated[str | None, Query()] = None,
) -> Response:
    storage = request.app.state.object_storage
    if settings.object_storage_impl == "selectel_s3":
        ref = await resolve_ref(
            uow.repositories.media,
            settings.internal_api_auth_secret,
            UgcSubmissionId(submission_id),
            t,
            settings.ugc_preview_ttl_seconds,
        )
        url = storage.presigned_get(ref.bucket, ref.key, settings.ugc_preview_ttl_seconds)
        return RedirectResponse(url, status_code=302, headers=_MEDIA_HEADERS)
    media = await resolve_bytes(
        uow.repositories.media,
        storage,
        settings.internal_api_auth_secret,
        UgcSubmissionId(submission_id),
        t,
        settings.ugc_preview_ttl_seconds,
    )
    return Response(content=media.data, media_type=media.content_type, headers=_MEDIA_HEADERS)


@router.get("/media/frame/{frame_submission_id}", include_in_schema=False)
async def get_frame_media(
    frame_submission_id: int,
    request: Request,
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
    t: Annotated[str | None, Query()] = None,
) -> Response:
    """Результат Polaroid по подписанной ссылке (B4): JPEG кодом 200 или 302 на presigned в S3."""
    storage = request.app.state.object_storage
    if settings.object_storage_impl == "selectel_s3":
        ref = await resolve_frame_ref(
            uow.repositories.frames,
            settings.internal_api_auth_secret,
            frame_submission_id,
            t,
            settings.media_frame_ttl_seconds,
        )
        url = storage.presigned_get(ref.bucket, ref.key, settings.media_frame_ttl_seconds)
        return RedirectResponse(url, status_code=302, headers=_MEDIA_HEADERS)
    media = await resolve_frame_bytes(
        uow.repositories.frames,
        storage,
        settings.internal_api_auth_secret,
        frame_submission_id,
        t,
        settings.media_frame_ttl_seconds,
    )
    return Response(content=media.data, media_type=media.content_type, headers=_MEDIA_HEADERS)
