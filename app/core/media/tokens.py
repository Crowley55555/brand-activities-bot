"""Подписанные токены ссылок на фото (П2 фазы 3b, B4 фазы 5).

itsdangerous.URLSafeTimedSerializer с ключом INTERNAL_API_AUTH_SECRET и отдельной солью на каждый
вид
ссылки: фото подачи UGC (ugc-media-token-v1, payload sid) и результат Polaroid
(frame-media-token-v1,
payload fid). Подпись одной солью не принимается другим маршрутом, даже при совпадении id.
Срок проверяется при разборе (max_age). Ключ передаёт вызывающий из конфига. Без ключа токены не
выдаются и не принимаются (fail-closed).
"""

from __future__ import annotations

from typing import Any, Final

from itsdangerous import BadData, URLSafeTimedSerializer

_SALT: Final[str] = "ugc-media-token-v1"
_PAYLOAD_KEY: Final[str] = "sid"
_FRAME_SALT: Final[str] = "frame-media-token-v1"
_FRAME_PAYLOAD_KEY: Final[str] = "fid"


def _serializer(hmac_key: str, salt: str = _SALT) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(hmac_key, salt=salt)


def sign_media_token(hmac_key: str, submission_id: int) -> str:
    """Токен на подачу. Без ключа - RuntimeError: ошибка конфигурации, как в local_fs."""
    if not hmac_key:
        raise RuntimeError("media_signing_key_missing")
    return _serializer(hmac_key).dumps({_PAYLOAD_KEY: int(submission_id)})


def verify_media_token(
    hmac_key: str, submission_id: int, ticket: str | None, max_age_seconds: int
) -> bool:
    """Токен выдан именно для этой подачи и не старше max_age_seconds. Любой сбой - False."""
    if not hmac_key or not ticket:
        return False
    try:
        payload: Any = _serializer(hmac_key).loads(ticket, max_age=max_age_seconds)
    except BadData:
        return False
    return isinstance(payload, dict) and payload.get(_PAYLOAD_KEY) == int(submission_id)


def sign_frame_media_token(hmac_key: str, frame_submission_id: int) -> str:
    """Токен результата Polaroid (B4). Без ключа - RuntimeError (fail-closed)."""
    if not hmac_key:
        raise RuntimeError("media_signing_key_missing")
    return _serializer(hmac_key, _FRAME_SALT).dumps(
        {_FRAME_PAYLOAD_KEY: int(frame_submission_id)}
    )


def verify_frame_media_token(
    hmac_key: str, frame_submission_id: int, ticket: str | None, max_age_seconds: int
) -> bool:
    """Токен выдан именно для этого результата и не старше max_age_seconds. Любой сбой - False."""
    if not hmac_key or not ticket:
        return False
    try:
        payload: Any = _serializer(hmac_key, _FRAME_SALT).loads(ticket, max_age=max_age_seconds)
    except BadData:
        return False
    return isinstance(payload, dict) and payload.get(_FRAME_PAYLOAD_KEY) == int(
        frame_submission_id
    )
