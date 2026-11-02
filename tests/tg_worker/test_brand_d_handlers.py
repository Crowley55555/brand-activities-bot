"""Тексты ответов tg_worker на ошибки ядра фазы 5 (колесо, Polaroid, подарок за пост).

Тестируем чистые функции: aiogram и сеть не нужны. Ошибка ядра переводится в понятный гостю текст,
внутренние коды (например, prizes_out_of_stock) наружу не уходят, неизвестная ошибка - общий ответ.
"""

from __future__ import annotations

from tg_worker.handlers.confirmation import CONFIRMATION_KIND, confirmation_error_text
from tg_worker.handlers.photo_frame import PHOTO_FRAME_KIND, photo_error_text
from tg_worker.handlers.wheel import WHEEL_KIND, wheel_error_text


def test_brand_d_kinds_match_core_activity_kinds() -> None:
    assert WHEEL_KIND == "wheel"
    assert PHOTO_FRAME_KIND == "photo_frame"
    assert CONFIRMATION_KIND == "confirmation"


def test_wheel_error_text_maps_known_codes_and_hides_unknown() -> None:
    assert "уже крутили" in wheel_error_text("already_spun_today")
    assert "закончились" in wheel_error_text("prizes_out_of_stock")
    assert "/start" in wheel_error_text("consent_required")
    fallback = wheel_error_text("some_internal_code")
    assert "some_internal_code" not in fallback
    assert "Не получилось" in fallback


def test_photo_error_text_asks_for_another_photo_on_file_problems() -> None:
    assert "другое фото" in photo_error_text("file_too_large")
    assert "другое фото" in photo_error_text("mime_not_allowed")
    assert "уже отправляли" in photo_error_text("already_passed_today")
    assert "Не получилось" in photo_error_text("unexpected")


def test_confirmation_error_text_covers_consent_and_unknown() -> None:
    assert "/start" in confirmation_error_text("consent_required")
    assert "уже отправляли" in confirmation_error_text("already_passed_today")
    assert "Не получилось" in confirmation_error_text("unexpected")
