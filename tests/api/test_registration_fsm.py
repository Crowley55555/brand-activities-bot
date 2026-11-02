from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.api.conftest import TEST_HMAC


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


def _start_update(tg_user_id: int = 12345) -> bytes:
    update = {
        "update": {
            "update_id": 10001,
            "message": {
                "message_id": 1,
                "date": 1760212800,
                "chat": {"id": tg_user_id, "type": "private"},
                "from": {
                    "id": tg_user_id,
                    "is_bot": False,
                    "username": "anna",
                },
                "text": "/start",
            },
        },
        "received_at": "2026-10-12T05:00:00+00:00",
    }
    return json.dumps(update).encode("utf-8")


def _pii_payload(
    external_id: str = "12345",
    first_name: str = "Иван",
    last_name: str = "Петров",
    phone: str = "+7 (900) 123-45-67",
    email: str = "ivan@example.com",
    policy_version: str = "v1",
) -> bytes:
    return json.dumps(
        {
            "channel_type": "telegram",
            "external_id": external_id,
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "email": email,
            "policy_version": policy_version,
        }
    ).encode("utf-8")


async def test_register_without_consent_rejects_409(
    api_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    # Шаг 1: user ещё не создан — ожидаем 404.
    body = _pii_payload()
    resp = await api_client.post(
        "/internal/v1/users/pii/update", content=body, headers=_auth(body)
    )
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"error": "channel_not_found"}

    # Шаг 2: пришёл /start — создался пустой user, но consent не принят.
    start_body = _start_update()
    resp = await api_client.post(
        "/internal/v1/channels/telegram/updates",
        content=start_body,
        headers=_auth(start_body),
    )
    assert resp.status_code == 200, resp.text

    # Шаг 3: без consent — 409.
    body = _pii_payload()
    resp = await api_client.post(
        "/internal/v1/users/pii/update", content=body, headers=_auth(body)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"error": "consent_required"}

    # Шаг 4: инвариант — ПДн в БД не записались.
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT first_name, last_name, phone, email FROM users")
        ).one()
        assert row.first_name is None
        assert row.last_name is None
        assert row.phone is None
        assert row.email is None


async def test_register_with_consent_persists_pii(
    api_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    # Шаг 1: /start — создаём user.
    start_body = _start_update()
    resp = await api_client.post(
        "/internal/v1/channels/telegram/updates",
        content=start_body,
        headers=_auth(start_body),
    )
    assert resp.status_code == 200, resp.text

    # Шаг 2: симулируем нажатие «Принимаю политику» —
    # напрямую вставляем consent в БД.
    now = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    fake_hash = "a" * 64
    with sync_engine.begin() as conn:
        user_id = conn.execute(text("SELECT id FROM users LIMIT 1")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO consents "
                "(user_id, policy_version, text_hash, accepted_at, channel_type) "
                "VALUES (:uid, 'v1', :h, :ts, 'telegram')"
            ),
            {"uid": user_id, "h": fake_hash, "ts": now},
        )

    # Шаг 3: отправка ПДн.
    body = _pii_payload()
    resp = await api_client.post(
        "/internal/v1/users/pii/update", content=body, headers=_auth(body)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["user_id"] == user_id

    # Шаг 4: ПДн записались, phone — E.164, email — lowercase.
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT first_name, last_name, phone, email, registered_at "
                "FROM users"
            )
        ).one()
        assert row.first_name == "Иван"
        assert row.last_name == "Петров"
        assert row.phone == "+79001234567"
        assert row.email == "ivan@example.com"
        assert row.registered_at is not None


async def test_pii_update_response_body_has_no_pii(
    api_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    """Ответ /pii/update никогда не содержит ПДн пользователя."""
    start_body = _start_update()
    await api_client.post(
        "/internal/v1/channels/telegram/updates",
        content=start_body,
        headers=_auth(start_body),
    )
    now = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    fake_hash = "a" * 64
    with sync_engine.begin() as conn:
        user_id = conn.execute(text("SELECT id FROM users LIMIT 1")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO consents "
                "(user_id, policy_version, text_hash, accepted_at, channel_type) "
                "VALUES (:uid, 'v1', :h, :ts, 'telegram')"
            ),
            {"uid": user_id, "h": fake_hash, "ts": now},
        )
    body = _pii_payload()
    resp = await api_client.post(
        "/internal/v1/users/pii/update", content=body, headers=_auth(body)
    )
    assert resp.status_code == 200, resp.text
    text_body = resp.text
    assert "Иван" not in text_body
    assert "Петров" not in text_body
    assert "+79001234567" not in text_body
    assert "ivan@example.com" not in text_body
    assert "anna" not in text_body
