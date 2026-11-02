"""HTTP фазы 4 (ADR-36): /internal/v1/activities/booking/{list_slots,book,cancel}.

T11-T13 - по промту (T13 переименован в test_cancel_403_if_not_owned: тело и ADR-36 дают 403,
решение п.3 координатора). Остальные кейсы - статусы 404/409 из ADR-36 и consent-gate.
Идём через ASGI-клиент с HMAC-подписью X-Internal-Auth, часы API - NOW (manual_clock).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.api.conftest import TEST_HMAC
from tests.factories import create_guest
from tests.factories.booking import (
    BRAND_C_SLUG,
    NOW,
    SLOT_ONE_PM,
    appointment_row,
    book_as,
    clean_booking,
    make_slot,
)

LIST_PATH = "/internal/v1/activities/booking/list_slots"
BOOK_PATH = "/internal/v1/activities/booking/book"
CANCEL_PATH = "/internal/v1/activities/booking/cancel"


@pytest.fixture(autouse=True)
def _booking_clean(sync_engine: Engine) -> Any:
    clean_booking(sync_engine)
    yield


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


async def _post(
    api_client: AsyncClient, path: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    resp = await api_client.post(path, content=body, headers=_auth(body))
    return resp.status_code, resp.json()


async def test_list_slots_returns_only_today_bookable_free(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T11: из слотов сегодня (прошедший, занятый, свободный) и завтрашнего - только свободный."""
    viewer = create_guest(sync_engine, external_id="4001", now=NOW)
    other = create_guest(sync_engine, external_id="4002", now=NOW)
    past = make_slot(sync_engine, BRAND_C_SLUG, NOW - timedelta(hours=1))
    taken = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    free = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM + timedelta(minutes=30))
    make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM + timedelta(days=1))
    await book_as(uow_factory, other, BRAND_C_SLUG, taken)

    status, body = await _post(
        api_client,
        LIST_PATH,
        {
            "channel_type": "telegram",
            "external_id": viewer.external_id,
            "activity_slug": BRAND_C_SLUG,
        },
    )

    assert status == 200, body
    assert [s["slot_id"] for s in body["slots"]] == [free]
    assert body["slots"][0]["duration_minutes"] == 15
    assert past not in [s["slot_id"] for s in body["slots"]]


async def test_book_409_slot_taken(
    api_client: AsyncClient, clean_db: None, sync_engine: Engine
) -> None:
    """T12: второй гость на занятый слот получает 409 {"error": "slot_taken"}."""
    first = create_guest(sync_engine, external_id="4003", now=NOW)
    second = create_guest(sync_engine, external_id="4004", now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)

    status1, body1 = await _post(
        api_client,
        BOOK_PATH,
        {
            "channel_type": "telegram",
            "external_id": first.external_id,
            "activity_slug": BRAND_C_SLUG,
            "slot_id": slot_id,
        },
    )
    status2, body2 = await _post(
        api_client,
        BOOK_PATH,
        {
            "channel_type": "telegram",
            "external_id": second.external_id,
            "activity_slug": BRAND_C_SLUG,
            "slot_id": slot_id,
        },
    )

    assert status1 == 200, body1
    assert body1["status"] == "booked"
    assert appointment_row(sync_engine, int(body1["appointment_id"])).status == "booked"
    assert status2 == 409
    assert body2 == {"error": "slot_taken"}


async def test_cancel_403_if_not_owned(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T13: чужую запись отменить нельзя - 403 attempt_not_owned, запись не меняется."""
    owner = create_guest(sync_engine, external_id="4005", now=NOW)
    intruder = create_guest(sync_engine, external_id="4006", now=NOW)
    booked = await book_as(
        uow_factory, owner, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )

    status, body = await _post(
        api_client,
        CANCEL_PATH,
        {
            "channel_type": "telegram",
            "external_id": intruder.external_id,
            "appointment_id": int(booked.appointment_id),
        },
    )

    assert status == 403
    assert body == {"error": "attempt_not_owned"}
    assert appointment_row(sync_engine, int(booked.appointment_id)).status == "booked"


async def test_cancel_own_then_409_not_cancellable(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """ADR-36: владелец отменяет - 200 cancelled_by_user; повтор - 409 not_cancellable."""
    owner = create_guest(sync_engine, external_id="4007", now=NOW)
    booked = await book_as(
        uow_factory, owner, BRAND_C_SLUG, make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)
    )
    payload = {
        "channel_type": "telegram",
        "external_id": owner.external_id,
        "appointment_id": int(booked.appointment_id),
    }

    first_status, first_body = await _post(api_client, CANCEL_PATH, payload)
    second_status, second_body = await _post(api_client, CANCEL_PATH, payload)

    assert first_status == 200
    assert first_body == {"status": "cancelled_by_user"}
    assert second_status == 409
    assert second_body == {"error": "not_cancellable"}


async def test_cancel_404_for_unknown_appointment(
    api_client: AsyncClient, clean_db: None, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="4008", now=NOW)

    status, body = await _post(
        api_client,
        CANCEL_PATH,
        {"channel_type": "telegram", "external_id": guest.external_id, "appointment_id": 999_999},
    )

    assert status == 404
    assert body == {"error": "appointment_not_found"}


async def test_book_without_consent_409(
    api_client: AsyncClient, clean_db: None, sync_engine: Engine
) -> None:
    """Правило 1 через HTTP: без согласия 409 consent_required, записи нет."""
    guest = create_guest(sync_engine, external_id="4009", with_consent=False, now=NOW)
    slot_id = make_slot(sync_engine, BRAND_C_SLUG, SLOT_ONE_PM)

    status, body = await _post(
        api_client,
        BOOK_PATH,
        {
            "channel_type": "telegram",
            "external_id": guest.external_id,
            "activity_slug": BRAND_C_SLUG,
            "slot_id": slot_id,
        },
    )

    assert status == 409
    assert body == {"error": "consent_required"}
    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM appointments")).scalar_one()
    assert count == 0
