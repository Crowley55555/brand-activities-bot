from __future__ import annotations

import pytest

from app.common.errors import ValidationError
from app.core.users.pii_normalize import (
    normalize_email,
    normalize_name,
    normalize_phone,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+7 (900) 123-45-67", "+79001234567"),
        ("+7 900 123-45-67", "+79001234567"),
        ("+79001234567", "+79001234567"),
        ("89001234567", "+79001234567"),
    ],
)
def test_normalize_phone_valid(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "9001234567",  # 10 цифр, не 11
        "+1 212 555 7890",  # не +7
        "abcde",
    ],
)
def test_normalize_phone_invalid(raw: str) -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_phone(raw)
    assert exc.value.code == "phone_invalid"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ivan@example.com", "ivan@example.com"),
        ("  IVAN@Example.COM  ", "ivan@example.com"),
        ("x.y+z@a.b.c", "x.y+z@a.b.c"),
    ],
)
def test_normalize_email_valid(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "no-at-sign", "a@", "@b.c", "a@b"])
def test_normalize_email_invalid(raw: str) -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_email(raw)
    assert exc.value.code == "email_invalid"


def test_normalize_name_strips_and_passes() -> None:
    assert normalize_name("  Иван  ") == "Иван"


@pytest.mark.parametrize("raw", ["", "   ", "x" * 101])
def test_normalize_name_invalid(raw: str) -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_name(raw)
    assert exc.value.code == "name_invalid"
