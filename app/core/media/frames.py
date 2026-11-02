"""Ссылки на результат Polaroid (B4, ADR-40): /media/frame/<frame_submission_id>?t=<token>.

Маршрут отдаёт только результат (статус done). Подпись своя (frame-media-token-v1): токен фото
подачи
UGC на этот маршрут не действует. Срок проверяется при открытии по media_frame_ttl_seconds из
конфига;
ссылка для гостя строится в момент отправки уведомления (outbox), поэтому короткий TTL не мешает.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.common.errors import Forbidden, NotFound
from app.core.media.tokens import sign_frame_media_token, verify_frame_media_token
from app.core.media.types import MediaBytes
from app.core.ports.object_storage import ObjectStoragePort


@dataclass(frozen=True)
class FrameResultRef:
    """Адрес результата в хранилище: бакет и ключ наружу не отдаём (правило 9)."""

    frame_submission_id: int
    bucket: str
    key: str
    content_type: str


@runtime_checkable
class FrameMediaRepo(Protocol):
    async def get_result_ref(self, frame_submission_id: int) -> FrameResultRef | None:
        """Результат со статусом done; иначе None."""
        ...


def frame_preview_url(hmac_key: str, frame_submission_id: int) -> str:
    """Относительная ссылка на результат; абсолютный адрес добавляет outbox из PUBLIC_BASE_URL."""
    ticket = sign_frame_media_token(hmac_key, frame_submission_id)
    return f"/media/frame/{int(frame_submission_id)}?t={ticket}"


async def resolve_frame_ref(
    repo: FrameMediaRepo,
    hmac_key: str,
    frame_submission_id: int,
    ticket: str | None,
    max_age_seconds: int,
) -> FrameResultRef:
    if not verify_frame_media_token(hmac_key, frame_submission_id, ticket, max_age_seconds):
        raise Forbidden("media_token_invalid")
    ref = await repo.get_result_ref(frame_submission_id)
    if ref is None:
        raise NotFound("media_not_found")
    return ref


async def resolve_frame_bytes(
    repo: FrameMediaRepo,
    storage: ObjectStoragePort,
    hmac_key: str,
    frame_submission_id: int,
    ticket: str | None,
    max_age_seconds: int,
) -> MediaBytes:
    ref = await resolve_frame_ref(repo, hmac_key, frame_submission_id, ticket, max_age_seconds)
    data = await storage.get(ref.bucket, ref.key)
    return MediaBytes(data=data, content_type=ref.content_type)
