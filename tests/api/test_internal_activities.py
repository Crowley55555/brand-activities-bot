from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.api.conftest import TEST_HMAC

ACTIVITY_SLUG = "brand_a_skin_survey"
PROMOCODE_PATTERN = re.compile(r"BRAND_A-[A-Z0-9]{8}")

# Ответ на каждый вопрос по его позиции (сид 0003). Профиль гостя:
# skin_type=combo, age=18_25, sensitivity=no, oily_tzone=no, pigmentation=no.
# Подходят карточка «combo» (сид 0004) и карточка «без условий» -> две карточки.
ANSWER_BY_POSITION = {1: "combo", 2: "age_18_25", 3: "no", 4: "no", 5: "no"}
EXPECTED_CARDS = 2


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


async def _post(
    api_client: AsyncClient, path: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    resp = await api_client.post(path, content=body, headers=_auth(body))
    return resp.status_code, resp.json()


async def _guest_with_channel(api_client: AsyncClient, external_id: int) -> None:
    """Гость из /start: user + channel_account, согласия ещё нет."""
    update = {
        "update": {
            "update_id": external_id,
            "message": {
                "message_id": 1,
                "date": 1760212800,
                "chat": {"id": external_id, "type": "private"},
                "from": {"id": external_id, "is_bot": False, "username": "guest"},
                "text": "/start",
            },
        },
        "received_at": "2026-10-12T05:00:00+00:00",
    }
    status, body = await _post(api_client, "/internal/v1/channels/telegram/updates", update)
    assert status == 200, body


async def _accept_consent(api_client: AsyncClient, external_id: int) -> None:
    status, body = await _post(
        api_client,
        "/internal/v1/consent/accept",
        {"channel_type": "telegram", "external_id": str(external_id)},
    )
    assert status == 200, body


async def _start(api_client: AsyncClient, external_id: int) -> tuple[int, dict[str, Any]]:
    return await _post(
        api_client,
        "/internal/v1/activities/start",
        {
            "channel_type": "telegram",
            "external_id": str(external_id),
            "activity_slug": ACTIVITY_SLUG,
        },
    )


async def test_start_without_consent_returns_409(api_client: AsyncClient) -> None:
    await _guest_with_channel(api_client, 700_001)
    status, body = await _start(api_client, 700_001)
    assert status == 409
    assert body == {"error": "consent_required"}


async def test_start_without_user_returns_404(api_client: AsyncClient) -> None:
    status, body = await _start(api_client, 799_999)
    assert status == 404
    assert body == {"error": "channel_not_found"}


async def test_start_twice_same_day_409(api_client: AsyncClient) -> None:
    await _guest_with_channel(api_client, 700_002)
    await _accept_consent(api_client, 700_002)
    status, body = await _start(api_client, 700_002)
    assert status == 200, body
    status, body = await _start(api_client, 700_002)
    assert status == 409
    assert body == {"error": "already_passed_today"}


async def test_full_flow_returns_rewards(api_client: AsyncClient, sync_engine: Engine) -> None:
    await _guest_with_channel(api_client, 700_003)
    await _accept_consent(api_client, 700_003)
    status, started = await _start(api_client, 700_003)
    assert status == 200, started
    attempt_id = started["attempt_id"]
    question = started["question"]

    is_last = False
    for _ in range(started["total_questions"]):
        assert question is not None
        status, answered = await _post(
            api_client,
            "/internal/v1/activities/answer",
            {
                "channel_type": "telegram",
                "external_id": str(700_003),
                "attempt_id": attempt_id,
                "question_id": question["id"],
                "selected_option_keys": [ANSWER_BY_POSITION[question["position"]]],
            },
        )
        assert status == 200, answered
        is_last = answered["is_last"]
        question = answered["next_question"]
    assert is_last is True
    assert question is None

    status, finished = await _post(
        api_client,
        "/internal/v1/activities/finish",
        {"channel_type": "telegram", "external_id": str(700_003), "attempt_id": attempt_id},
    )
    assert status == 200, finished
    assert finished["promocode"] is not None
    assert PROMOCODE_PATTERN.fullmatch(finished["promocode"])

    rewards = {reward["reward_type"]: reward for reward in finished["rewards"]}
    assert set(rewards) == {"recommendation_set", "promocode"}
    assert rewards["promocode"]["code"] == finished["promocode"]
    card_ids = rewards["recommendation_set"]["product_recommendation_ids"]
    assert len(card_ids) == EXPECTED_CARDS
    assert len(finished["recommendations"]) == EXPECTED_CARDS

    with sync_engine.connect() as conn:
        status_db = conn.execute(
            text("SELECT status FROM attempts WHERE id = :id"), {"id": attempt_id}
        ).scalar_one()
        assert status_db == "completed"
        n_rewards = conn.execute(
            text("SELECT COUNT(*) FROM rewards WHERE attempt_id = :id"), {"id": attempt_id}
        ).scalar_one()
        assert n_rewards == 2


async def test_answer_from_other_user_returns_403(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    """Чужой гость с валидным HMAC не отвечает на чужое прохождение (владелец проверяется)."""
    await _guest_with_channel(api_client, 700_010)
    await _accept_consent(api_client, 700_010)
    status, started = await _start(api_client, 700_010)
    assert status == 200, started
    await _guest_with_channel(api_client, 700_011)
    question = started["question"]

    status, body = await _post(
        api_client,
        "/internal/v1/activities/answer",
        {
            "channel_type": "telegram",
            "external_id": "700011",
            "attempt_id": started["attempt_id"],
            "question_id": question["id"],
            "selected_option_keys": [ANSWER_BY_POSITION[question["position"]]],
        },
    )

    assert status == 403
    assert body == {"error": "attempt_not_owned"}
    with sync_engine.connect() as conn:
        n_answers = conn.execute(text("SELECT COUNT(*) FROM survey_answers")).scalar_one()
    assert n_answers == 0


async def test_finish_from_other_user_returns_403(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    """Чужой гость не может закрыть чужое прохождение: 403, статус остаётся in_progress."""
    await _guest_with_channel(api_client, 700_012)
    await _accept_consent(api_client, 700_012)
    status, started = await _start(api_client, 700_012)
    assert status == 200, started
    await _guest_with_channel(api_client, 700_013)

    status, body = await _post(
        api_client,
        "/internal/v1/activities/finish",
        {
            "channel_type": "telegram",
            "external_id": "700013",
            "attempt_id": started["attempt_id"],
        },
    )

    assert status == 403
    assert body == {"error": "attempt_not_owned"}
    with sync_engine.connect() as conn:
        status_db = conn.execute(
            text("SELECT status FROM attempts WHERE id = :id"), {"id": started["attempt_id"]}
        ).scalar_one()
    assert status_db == "in_progress"


async def test_answer_without_channel_returns_404(api_client: AsyncClient) -> None:
    """Канал отправителя должен существовать, прежде чем проверять прохождение."""
    status, body = await _post(
        api_client,
        "/internal/v1/activities/answer",
        {
            "channel_type": "telegram",
            "external_id": "799998",
            "attempt_id": 1,
            "question_id": 1,
            "selected_option_keys": ["combo"],
        },
    )

    assert status == 404
    assert body == {"error": "channel_not_found"}
