"""Фото UGC для превью модератора и для раздачи по ссылке /media (фаза 3b, П2).

preview_url собирает ссылку на маршрут /media с подписанным токеном. resolve_bytes проверяет токен,
достаёт адрес файла из репозитория и читает байты через порт хранилища. HTTP-ответ собирает слой
api (правило 3): ядро про заголовки не знает. В фазе 3c S3 ответит 302 через presigned_get; этот
сервис при этом не меняется, меняется только адаптер хранилища и слой api.
"""

from __future__ import annotations

from app.common.errors import Forbidden, NotFound
from app.core.media.repository import MediaRepo
from app.core.media.tokens import sign_media_token, verify_media_token
from app.core.media.types import MediaBytes, SubmissionFileRef
from app.core.ports.object_storage import ObjectStoragePort
from app.core.types.ids import UgcSubmissionId


def preview_url(hmac_key: str, submission_id: int) -> str:
    """Ссылка на фото подачи: /media/<submission_id>?t=<token>. Срок проверяется при открытии."""
    return f"/media/{int(submission_id)}?t={sign_media_token(hmac_key, submission_id)}"


async def resolve_ref(
    repo: MediaRepo,
    hmac_key: str,
    submission_id: UgcSubmissionId,
    ticket: str | None,
    max_age_seconds: int,
) -> SubmissionFileRef:
    """Адрес файла по подписанной ссылке: для S3 маршрут отвечает 302 (фаза 3c, п.13)."""
    if not verify_media_token(hmac_key, int(submission_id), ticket, max_age_seconds):
        raise Forbidden("media_token_invalid")
    ref = await repo.get_submission_by_id(submission_id)
    if ref is None:
        raise NotFound("media_not_found")
    return ref


async def resolve_bytes(
    repo: MediaRepo,
    storage: ObjectStoragePort,
    hmac_key: str,
    submission_id: UgcSubmissionId,
    ticket: str | None,
    max_age_seconds: int,
) -> MediaBytes:
    """Байты фото по подписанной ссылке. Токен отсутствует, чужой или истёк - Forbidden."""
    if not verify_media_token(hmac_key, int(submission_id), ticket, max_age_seconds):
        raise Forbidden("media_token_invalid")
    ref = await repo.get_submission_by_id(submission_id)
    if ref is None:
        raise NotFound("media_not_found")
    data = await storage.get(ref.bucket, ref.key)
    return MediaBytes(data=data, content_type=ref.content_type)
