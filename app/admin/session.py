from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Final

from itsdangerous import BadData, URLSafeTimedSerializer

from app.admin.cookies import SESSION_MAX_AGE
from app.core.admin_access import SessionPayload
from app.core.types.ids import AdminId

# Соль разделяет подписи сессии от любых других подписей на том же ключе.
SESSION_SALT: Final = "admin.session.v1"
# Плейсхолдер из .env.example (`<ADMIN_SESSION_SECRET>`), скопированный в .env
# без замены, - публично известное значение, подписывать им нельзя.
_PLACEHOLDER: Final = re.compile(r"<[A-Z_][A-Z0-9_]*>")
# Реальная подписанная сессия ~140 символов; больше - заведомо мусор.
_MAX_COOKIE_LENGTH: Final = 2048


def is_secret_usable(signing_key: str) -> bool:
    """Ключ задан и не является плейсхолдером (иначе /admin/* закрыт, 503)."""
    return bool(signing_key) and _PLACEHOLDER.fullmatch(signing_key) is None


def _serializer(signing_key: str) -> URLSafeTimedSerializer:
    # itsdangerous по умолчанию подписывает HMAC-SHA1; ADR-9 требует
    # HMAC-SHA256, поэтому digest_method задаём явно.
    return URLSafeTimedSerializer(
        signing_key,
        salt=SESSION_SALT,
        signer_kwargs={"digest_method": hashlib.sha256, "key_derivation": "hmac"},
    )


def sign_session(payload: SessionPayload, signing_key: str) -> str:
    if not is_secret_usable(signing_key):
        raise RuntimeError("ADMIN_SESSION_SECRET is not configured")
    return _serializer(signing_key).dumps(
        {
            "admin_id": int(payload.admin_id),
            "login": payload.login,
            "issued_at": payload.issued_at.isoformat(),
        }
    )


def load_session(value: str, signing_key: str) -> SessionPayload | None:
    """Проверяет подпись и срок (8ч); любой сбой -> None, исключений наружу нет."""
    if not is_secret_usable(signing_key) or len(value) > _MAX_COOKIE_LENGTH:
        return None
    try:
        data = _serializer(signing_key).loads(value, max_age=SESSION_MAX_AGE)
    except BadData:  # BadSignature, SignatureExpired, BadPayload...
        return None
    if not isinstance(data, dict):
        return None
    admin_id = data.get("admin_id")
    login = data.get("login")
    issued = data.get("issued_at")
    if type(admin_id) is not int or not isinstance(login, str) or not isinstance(issued, str):
        return None
    try:
        issued_at = datetime.fromisoformat(issued)
    except ValueError:
        return None
    return SessionPayload(admin_id=AdminId(admin_id), login=login, issued_at=issued_at)
