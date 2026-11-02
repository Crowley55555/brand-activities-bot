"""tg_worker: выбор booking-активности, время слота по Москве и длина callback-данных (ADR-37)."""

from __future__ import annotations

from tg_worker.handlers.booking import _time_label, pick_booking_activity


def test_pick_booking_activity_skips_other_kinds() -> None:
    activities = [
        {"slug": "brand_a_skin_survey", "kind": "survey"},
        {"slug": "brand_c_stylist_booking", "kind": "booking"},
    ]

    picked = pick_booking_activity(activities)

    assert picked is not None
    assert picked["slug"] == "brand_c_stylist_booking"


def test_pick_booking_activity_none_without_booking() -> None:
    assert pick_booking_activity([{"slug": "x", "kind": "survey"}]) is None


def test_time_label_is_moscow_time() -> None:
    # 10:00 UTC = 13:00 по Москве
    assert _time_label("2026-10-12T10:00:00+00:00") == "13:00"


def test_slot_callback_data_fits_telegram_limit() -> None:
    # Telegram принимает callback_data до 64 байт: slot:<id>:<start_at ISO>
    data = "slot:4294967295:2026-10-12T10:00:00+00:00"
    assert len(data.encode("utf-8")) <= 64
