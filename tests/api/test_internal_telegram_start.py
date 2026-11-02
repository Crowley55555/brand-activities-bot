from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.api.conftest import TEST_HMAC, ManualClock


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


async def test_second_start_returns_same_user_without_pii(
    api_client: AsyncClient,
    manual_clock: ManualClock,
    sync_engine: Engine,
) -> None:
    update_body = {
        "update": {
            "update_id": 10001,
            "message": {
                "message_id": 1,
                "date": 1760212800,
                "chat": {"id": 12345, "type": "private"},
                "from": {
                    "id": 12345,
                    "is_bot": False,
                    "username": "anna",
                    "first_name": "Anna",
                },
                "text": "/start",
            },
        },
        "received_at": "2026-10-12T05:00:00+00:00",
    }
    body_bytes = json.dumps(update_body).encode("utf-8")
    resp = await api_client.post(
        "/internal/v1/channels/telegram/updates",
        content=body_bytes,
        headers=_auth(body_bytes),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    # Ядро не формирует приветственный reply — tg-адаптер сам выберет
    # сценарий (consent-FSM или меню) по is_registered.
    assert data["reply"] is None
    assert data["is_registered"] is False

    with sync_engine.connect() as conn:
        n_channels = conn.execute(
            text(
                "SELECT COUNT(*) FROM channel_accounts "
                "WHERE channel_type='telegram' AND external_id='12345' "
                "AND unlinked_at IS NULL"
            )
        ).scalar_one()
        assert n_channels == 1

        n_users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        assert n_users == 1

        row = conn.execute(
            text(
                "SELECT first_name, last_name, phone, email, registered_at "
                "FROM users"
            )
        ).one()
        assert row.first_name is None
        assert row.last_name is None
        assert row.phone is None
        assert row.email is None
        assert row.registered_at is None

        n_consents = conn.execute(
            text("SELECT COUNT(*) FROM consents")
        ).scalar_one()
        assert n_consents == 0

    manual_clock.set(datetime(2026, 10, 12, 5, 2, tzinfo=UTC))
    update_body["update"]["update_id"] = 10002
    update_body["received_at"] = "2026-10-12T05:02:00+00:00"
    body_bytes = json.dumps(update_body).encode("utf-8")
    resp = await api_client.post(
        "/internal/v1/channels/telegram/updates",
        content=body_bytes,
        headers=_auth(body_bytes),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["reply"] is None
    # ПДн ещё не записаны (consent не принят в этом тесте), поэтому is_registered=False
    assert data["is_registered"] is False

    with sync_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM channel_accounts")).scalar_one()
            == 1
        )
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1
        last_vs_created = conn.execute(
            text("SELECT last_seen_at > created_at FROM users")
        ).scalar_one()
        assert last_vs_created is True
        row = conn.execute(
            text("SELECT first_name, last_name, phone, email FROM users")
        ).one()
        assert row.first_name is None
        assert row.last_name is None
        assert row.phone is None
        assert row.email is None


async def test_non_start_message_creates_nothing(
    api_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    update_body = {
        "update": {
            "update_id": 99999,
            "message": {
                "message_id": 10,
                "date": 1760212800,
                "chat": {"id": 99999, "type": "private"},
                "from": {
                    "id": 99999,
                    "is_bot": False,
                    "username": "stranger",
                },
                "text": "привет",
            },
        },
        "received_at": "2026-10-12T05:00:00+00:00",
    }
    body_bytes = json.dumps(update_body).encode("utf-8")
    resp = await api_client.post(
        "/internal/v1/channels/telegram/updates",
        content=body_bytes,
        headers=_auth(body_bytes),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["reply"] is None

    with sync_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM channel_accounts")).scalar_one()
            == 0
        )
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 0
