from __future__ import annotations

from typing import Final, Literal

from starlette.responses import Response

SESSION_COOKIE: Final = "admin_session"  # ADR-9
CSRF_COOKIE: Final = "admin_csrf"  # ADR-10
COOKIE_PATH: Final = "/admin"
SESSION_MAX_AGE: Final = 28800  # 8 часов (ADR-9)

SameSite = Literal["Lax", "Strict"]

_EPOCH: Final = "Thu, 01 Jan 1970 00:00:00 GMT"


def _check_value(value: str) -> None:
    # Подписанная сессия (urlsafe base64 + '.') и hex-токен проходят без
    # кавычек; всё остальное - ошибка программиста, а не повод дописывать
    # экранирование (и защита от инъекции в заголовок).
    if not value or any(not 0x21 <= ord(ch) <= 0x7E or ch in '";,\\' for ch in value):
        raise ValueError("cookie value must be non-empty printable ASCII without ;,\"\\")


def set_cookie(
    response: Response,
    name: str,
    value: str,
    *,
    max_age: int,
    secure: bool,
    httponly: bool,
    samesite: SameSite,
) -> None:
    _check_value(value)
    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={COOKIE_PATH}"]
    if httponly:
        parts.append("HttpOnly")
    parts.append(f"SameSite={samesite}")
    if secure:
        parts.append("Secure")
    response.headers.append("set-cookie", "; ".join(parts))


def clear_cookie(
    response: Response,
    name: str,
    *,
    secure: bool,
    httponly: bool,
    samesite: SameSite,
) -> None:
    parts = [f"{name}=", f"Expires={_EPOCH}", "Max-Age=0", f"Path={COOKIE_PATH}"]
    if httponly:
        parts.append("HttpOnly")
    parts.append(f"SameSite={samesite}")
    if secure:
        parts.append("Secure")
    response.headers.append("set-cookie", "; ".join(parts))
