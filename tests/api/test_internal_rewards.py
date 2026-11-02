"""/internal/v1/rewards/* (фаза 10b-1, C-1): OTP для именных наград.

Правило 10 (auth): все методы под HMAC — без заголовка 401. Правило 4 (внешние
ресурсы через порт): tg_worker ходит сюда HTTP, не в БД.

Падающие тесты из §4 промта:
* 14 — request_otp без HMAC → 401;
* 15 — успех: 200 {otp: "...", expires_at: "...", ttl_minutes};
* 16 — на predъявительский (promocode) → 409 reward_not_eligible.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import TEST_HMAC
from tests.factories import create_attempt, create_guest

# ManualClock в tests/conftest.py фиксирован на 2026-10-12 05:00 UTC.
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


def _sign(body: bytes) -> str:
    return hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_body(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode("utf-8")
    return body, {
        "Content-Type": "application/json",
        "X-Internal-Auth": _sign(body),
    }


def _create_voucher(engine: Engine, attempt_id: int, code: str = "VOUCHER-R01") -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO rewards "
                "(attempt_id, reward_type, code, title, issued_at, is_bearer, "
                " created_at, updated_at) "
                "VALUES (:aid, 'voucher', :code, :title, :ts, false, :ts, :ts) "
                "RETURNING id"
            ),
            {"aid": attempt_id, "code": code, "title": "Напиток дня", "ts": NOW},
        ).one()
    return int(row.id)


def _create_promocode(engine: Engine, attempt_id: int, code: str = "PROMO-R01") -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO rewards "
                "(attempt_id, reward_type, code, issued_at, is_bearer, "
                " created_at, updated_at) "
                "VALUES (:aid, 'promocode', :code, :ts, true, :ts, :ts) "
                "RETURNING id"
            ),
            {"aid": attempt_id, "code": code, "ts": NOW},
        ).one()
    return int(row.id)


# ----- 14. HMAC обязателен ----------------------------------------------------


async def test_request_otp_requires_hmac(api_client: AsyncClient) -> None:
    body = json.dumps({"channel_type": "telegram", "external_id": "8001"}).encode("utf-8")
    resp = await api_client.post(
        "/internal/v1/rewards/999/request_otp",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


# ----- 15. успех -------------------------------------------------------------


async def test_request_otp_success(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="8002", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt, code="VOUCHER-R15")

    body, headers = _post_body({"channel_type": "telegram", "external_id": "8002"})
    resp = await api_client.post(
        f"/internal/v1/rewards/{voucher_id}/request_otp",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert isinstance(out["otp"], str) and len(out["otp"]) == 6
    assert out["otp"].isdigit()
    assert "expires_at" in out
    assert out["ttl_minutes"] >= 1

    # OTP записался в БД
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT redeem_otp FROM rewards WHERE id = :id"),
            {"id": voucher_id},
        ).one()
    assert row.redeem_otp == out["otp"]


# ----- 16. на promocode -> 409 reward_not_eligible ----------------------------


async def test_request_otp_not_eligible_409(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, external_id="8003", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    promocode_id = _create_promocode(sync_engine, attempt, code="PROMO-R16")

    body, headers = _post_body({"channel_type": "telegram", "external_id": "8003"})
    resp = await api_client.post(
        f"/internal/v1/rewards/{promocode_id}/request_otp",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"error": "reward_not_eligible"}


# ----- Доп: list_active_rewards -----------------------------------------------


async def test_list_active_requires_hmac(api_client: AsyncClient) -> None:
    body = json.dumps({"channel_type": "telegram", "external_id": "8004"}).encode("utf-8")
    resp = await api_client.post(
        "/internal/v1/rewards/list_active",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_list_active_returns_named_rewards(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    """В ответе только voucher/physical_gift, не промокоды."""
    guest = create_guest(sync_engine, external_id="8005", now=NOW)
    attempt = create_attempt(sync_engine, guest, "brand_b_photo_subscription")
    voucher_id = _create_voucher(sync_engine, attempt, code="VOUCHER-R17a")
    _create_promocode(sync_engine, attempt, code="PROMO-R17a")  # не должна появиться

    body, headers = _post_body({"channel_type": "telegram", "external_id": "8005"})
    resp = await api_client.post(
        "/internal/v1/rewards/list_active",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [r["reward_id"] for r in data["rewards"]] == [voucher_id]
    assert data["rewards"][0]["reward_type"] == "voucher"
    assert data["rewards"][0]["title"] == "Напиток дня"
