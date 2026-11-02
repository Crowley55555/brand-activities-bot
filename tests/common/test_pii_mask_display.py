from __future__ import annotations

from app.common.pii_display import mask_email, mask_name, mask_phone


def test_mask_phone_e164() -> None:
    assert mask_phone("+79161234567") == "+79***4567"


def test_mask_email() -> None:
    assert mask_email("artem@example.com") == "a***@e***.com"


def test_mask_name() -> None:
    assert mask_name("Артём") == "А***"


def test_mask_handles_none_and_short_inputs() -> None:
    assert mask_name(None) == "—"
    assert mask_name("") == "—"
    assert mask_phone(None) == "—"
    assert mask_phone("+7123") == "***"
    assert mask_email(None) == "—"
    assert mask_email("not-an-email") == "***"
    assert mask_email("a@b") == "***"
