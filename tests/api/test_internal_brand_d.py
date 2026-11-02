"""HTTP фазы 5 (ADR-39, ADR-42): POST /internal/v1/activities/{wheel/spin, confirmation/accept}.

T15 - по промту: валидный запрос спина -> 200 {prize_title, reward_id}. Все запросы подписаны
X-Internal-Auth (HMAC), как у booking. Часы API - системные: дата спина не проверяется.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from httpx import AsyncClient
from sqlalchemy.engine import Engine

from tests.api.conftest import TEST_HMAC
from tests.factories import create_guest
from tests.factories.brand_d import (
    CONFIRMATION_LIMITS,
    WHEEL_LIMITS,
    make_activity,
    make_prize,
)

SPIN_PATH = "/internal/v1/activities/wheel/spin"
CONFIRM_PATH = "/internal/v1/activities/confirmation/accept"


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


async def _post(api_client: AsyncClient, path: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return await api_client.post(path, content=body, headers=_auth(body))


async def test_spin_endpoint_returns_prize(api_client: AsyncClient, sync_engine: Engine) -> None:
    """T15: spin по HTTP -> 200 {prize_title, reward_id}."""
    create_guest(sync_engine, external_id="3301")
    activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    make_prize(sync_engine, activity.activity_id, "Комплимент стилиста", "compliment", 1)

    response = await _post(
        api_client,
        SPIN_PATH,
        {"channel_type": "telegram", "external_id": "3301", "activity_slug": activity.slug},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["prize_title"] == "Комплимент стилиста"
    assert isinstance(body["reward_id"], int)


async def test_confirmation_accept_endpoint_creates_pending(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    """HTTP подарка за пост: 200, статус pending, файла нет (ADR-42)."""
    create_guest(sync_engine, external_id="3302")
    activity = make_activity(sync_engine, "confirmation", CONFIRMATION_LIMITS)

    response = await _post(
        api_client,
        CONFIRM_PATH,
        {
            "channel_type": "telegram",
            "external_id": "3302",
            "activity_slug": activity.slug,
            "post_url": "https://www.instagram.com/p/brand-d-http/",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
