"""POST /internal/v1/users/forget (фаза 10b-3, I-2): HMAC-защищённый каскад.

Правило 10: без HMAC — 401. Правило 4: tg_worker ходит сюда, в БД не ходит.
Правило 2: ответ содержит только счётчики (ПДн нет).
"""

from __future__ import annotations

import hashlib
import hmac
import json

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import TEST_HMAC
from tests.factories import create_guest
from tests.factories.ugc import create_admin  # noqa: F401 (на будущее)


def _sign(body: bytes) -> str:
    return hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode("utf-8")
    return body, {
        "Content-Type": "application/json",
        "X-Internal-Auth": _sign(body),
    }


async def test_forget_requires_hmac(api_client: AsyncClient, clean_db: None) -> None:
    """POST без X-Internal-Auth → 401 missing_auth."""
    body = json.dumps({"channel_type": "telegram", "external_id": "7001"})
    resp = await api_client.post(
        "/internal/v1/users/forget",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_forget_full_flow(
    api_client: AsyncClient, clean_db: None, sync_engine: Engine
) -> None:
    """С HMAC → 200 + анонимизация в БД + revoke consent + delete FSM."""
    guest = create_guest(sync_engine, external_id="9001")

    # Регистрация ПДн напрямую, чтобы is_registered=True.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET first_name=:fn, last_name=:ln, phone=:ph, "
                "  email=:em, registered_at=now() WHERE id=:uid"
            ),
            {
                "fn": "Иван",
                "ln": "Петров",
                "ph": "+79001234567",
                "em": "ivan@example.com",
                "uid": guest.user_id,
            },
        )
        # FSM-строки гостя (миграция 0021: user_id).
        conn.execute(
            text(
                "INSERT INTO tg_fsm_state (key, state, data, updated_at, user_id) "
                "VALUES (:k, 'reg:wait_phone', '{\"first_name\":\"Иван\"}'::jsonb, "
                "  now(), :uid)"
            ),
            {"k": "bot:1:9001:::default", "uid": guest.user_id},
        )

    body, headers = _post({"channel_type": "telegram", "external_id": "9001"})
    resp = await api_client.post(
        "/internal/v1/users/forget", content=body, headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["user_id"] == guest.user_id
    assert data["tg_fsm_rows_deleted"] == 1
    assert data["ugc_submissions_deleted"] == 0
    assert data["s3_delete_jobs_enqueued"] == 0

    # БД: ПДн обнулены, anonymized_at проставлен.
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT first_name, last_name, phone, email, anonymized_at "
                "FROM users WHERE id = :uid"
            ),
            {"uid": guest.user_id},
        ).one()
    assert row.first_name is None
    assert row.last_name is None
    assert row.phone is None
    assert row.email is None
    assert row.anonymized_at is not None

    # Consent revoked.
    with sync_engine.connect() as conn:
        revoked = conn.execute(
            text(
                "SELECT revoked_at FROM consents WHERE user_id = :uid"
            ),
            {"uid": guest.user_id},
        ).scalar_one()
    assert revoked is not None

    # FSM-строк гостя не осталось.
    with sync_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM tg_fsm_state WHERE user_id = :uid"
            ),
            {"uid": guest.user_id},
        ).scalar_one()
    assert count == 0


async def test_forget_unknown_channel_returns_404(
    api_client: AsyncClient, clean_db: None
) -> None:
    body, headers = _post({"channel_type": "telegram", "external_id": "no-such"})
    resp = await api_client.post(
        "/internal/v1/users/forget", content=body, headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "channel_not_found"


async def test_forget_unregistered_user_returns_409(
    api_client: AsyncClient, clean_db: None, sync_engine: Engine
) -> None:
    """Гость создан (channel), но без ПДн → not_registered."""
    create_guest(sync_engine, external_id="9002", with_consent=False)
    body, headers = _post({"channel_type": "telegram", "external_id": "9002"})
    resp = await api_client.post(
        "/internal/v1/users/forget", content=body, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "not_registered"
