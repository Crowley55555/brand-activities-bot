from __future__ import annotations

import re
import secrets
import string
from collections.abc import Callable
from typing import Final

from app.common.errors import ValidationError

# ADR-13: код набирают у стойки, поэтому алфавит - только A-Z и 0-9 (36 символов,
# 36**8 ~ 2.8e12 вариантов на 8 знаков). `secrets.token_urlsafe(n).upper()` не
# годится: base64url содержит '-' и '_' и ломает формат ^[A-Z0-9]{n}$.
CODE_ALPHABET: Final[str] = string.ascii_uppercase + string.digits
MAX_RANDOM_LENGTH: Final[int] = 32

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{random(\d+)\}")

RandomSource = Callable[[int], str]


def default_random_source(length: int) -> str:
    """`length` символов алфавита CODE_ALPHABET из криптостойкого источника."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def generate_code(template: str, random_source: RandomSource | None = None) -> str:
    """Отрисовать шаблон кода: `<ПРЕФИКС>-{random8}` -> `<ПРЕФИКС>-K7Q2M9XA`.

    В шаблоне ровно один плейсхолдер `{randomN}`, N - длина случайной части
    (1..MAX_RANDOM_LENGTH). `random_source(N)` отдаёт случайную часть; по умолчанию
    криптостойкий источник по алфавиту [A-Z0-9], в тестах подставляется детерминированный.
    Шаблон хранится в данных активности (правило 5), а не в коде.
    """
    placeholders = _PLACEHOLDER.findall(template)
    if len(placeholders) != 1:
        raise ValidationError("code_template_invalid")
    length = int(placeholders[0])
    if not 1 <= length <= MAX_RANDOM_LENGTH:
        raise ValidationError("code_template_invalid")

    source = random_source if random_source is not None else default_random_source
    chunk = source(length)
    if len(chunk) != length:
        raise ValidationError("code_source_invalid")
    return _PLACEHOLDER.sub(lambda _match: chunk, template)
