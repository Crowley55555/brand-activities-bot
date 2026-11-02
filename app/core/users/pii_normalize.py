from __future__ import annotations

import re

from app.common.errors import ValidationError

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_name(raw: str) -> str:
    """Имя/фамилия: strip, запрет пустого, длина 1..100.

    Правило 5: тексты и границы — не в коде, но длина — это техническая
    валидация полей БД, а не бизнес-специфика бренда.
    """
    if not isinstance(raw, str):
        raise ValidationError("name_invalid")
    stripped = raw.strip()
    if not stripped or len(stripped) > 100:
        raise ValidationError("name_invalid")
    return stripped


def normalize_phone(raw: str) -> str:
    """Телефон → E.164 (+7 цифры).

    Разрешённые входы: `+7 (900) 123-45-67`, `+7 900 123-45-67`, `89001234567`,
    `+79001234567`. Всё остальное — ValidationError('phone_invalid').
    """
    if not isinstance(raw, str):
        raise ValidationError("phone_invalid")
    stripped = raw.strip()
    if not stripped:
        raise ValidationError("phone_invalid")
    starts_plus = stripped.startswith("+")
    digits = re.sub(r"\D", "", stripped)
    if starts_plus and len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    if not starts_plus and len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    raise ValidationError("phone_invalid")


def normalize_email(raw: str) -> str:
    """Email: strip + lower; простая проверка `name@host.tld`."""
    if not isinstance(raw, str):
        raise ValidationError("email_invalid")
    stripped = raw.strip().lower()
    if not _EMAIL_RE.match(stripped):
        raise ValidationError("email_invalid")
    return stripped
