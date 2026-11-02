"""Маскирование ПДн для показа в админке (фаза 7a).

Чистые функции без состояния. Отдельно от `pii_mask` (процессор логов): здесь маска
для человека, а не для структурированного лога.
"""

from __future__ import annotations

from typing import Final

_DASH: Final = "—"
_STARS: Final = "***"
_PHONE_HEAD: Final = 3
_PHONE_TAIL: Final = 4


def mask_name(name: str | None) -> str:
    """`Артём` -> `А***`; пусто -> тире."""
    if not name:
        return _DASH
    return f"{name[0]}{_STARS}"


def mask_phone(phone_e164: str | None) -> str:
    """`+79161234567` -> `+79***4567`; слишком короткий -> `***`."""
    if not phone_e164:
        return _DASH
    if len(phone_e164) < _PHONE_HEAD + _PHONE_TAIL + 1:
        return _STARS
    return f"{phone_e164[:_PHONE_HEAD]}{_STARS}{phone_e164[-_PHONE_TAIL:]}"


def mask_email(email: str | None) -> str:
    """`artem@example.com` -> `a***@e***.com`; невалидный -> `***`."""
    if not email:
        return _DASH
    local, sep, domain = email.partition("@")
    host, dot, tld = domain.rpartition(".")
    if not sep or not local or not dot or not host or not tld:
        return _STARS
    return f"{local[0]}{_STARS}@{host[0]}{_STARS}.{tld}"
