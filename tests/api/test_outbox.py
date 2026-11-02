"""HTTP outbox фазы 3c (ADR-32): /internal/v1/channels/telegram/outbox/{claim,ack}.

T12, T13 - приёмочные из промта. Текст собирается в claim (п.8 ревью): tg_worker видит готовый text.
ТОП-3: ack несёт worker_id (fencing); чужой воркер получает 409 fencing_failed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.jobs import enqueue
from tests.api.conftest import TEST_HMAC
from tests.factories import Guest
from tests.factories.ugc import create_admin
from tests.factories.workers import (
    approve_submission,
    channel_unlinked,
    ingest_done,
    brand_b_submission,
    reward_rows,
)

CLAIM = "/internal/v1/channels/telegram/outbox/claim"
ACK = "/internal/v1/channels/telegram/outbox/ack"
WORKER = "tg-test:1"


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


async def _post(api_client: AsyncClient, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    body = json.dumps(payload).encode("utf-8")
    resp = await api_client.post(path, content=body, headers=_auth(body))
    return resp.status_code, resp.json()


async def _approved_rewards_job(
    uow_factory: Any, engine: Engine, external_id: int
) -> tuple[Guest, list[Any]]:
    """Одобренная подача Бренд B и telegram_send(reward_notice) в очереди: гость и награды."""
    case = await brand_b_submission(uow_factory, engine, external_id)
    await ingest_done(uow_factory, case.submission_id)
    await approve_submission(uow_factory, case.submission_id, create_admin(engine))
    rewards = reward_rows(engine, case.attempt_id)
    voucher = next(r for r in rewards if r.reward_type == "voucher")
    promo = next(r for r in rewards if r.reward_type == "promocode")
    async with uow_factory() as uow:
        await enqueue(
            uow.repositories.jobs,
            "telegram_send",
            {
                "chat_id": int(case.guest.external_id),
                "template": "reward_notice",
                "context": {"reward_ids": [int(voucher.id), int(promo.id)]},
            },
            idempotency_key=f"reward_notice:{int(voucher.id)}",
        )
    return case.guest, rewards


async def _claim(api_client: AsyncClient, worker: str = WORKER) -> tuple[int, Any]:
    return await _post(
        api_client, CLAIM, {"worker_id": worker, "limit": 10, "visibility_timeout_s": 90}
    )


def _job_state(engine: Engine, kind: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, status, last_error, locked_by FROM jobs WHERE kind = :kind ORDER BY id"
            ),
            {"kind": kind},
        ).all()


async def test_outbox_claim_returns_telegram_send_jobs(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T12: claim отдаёт готовый текст с обоими кодами и названием ваучера, без template."""
    guest, rewards = await _approved_rewards_job(uow_factory, sync_engine, 9001)
    voucher = next(r for r in rewards if r.reward_type == "voucher")
    promo = next(r for r in rewards if r.reward_type == "promocode")

    status, body = await _claim(api_client)

    assert status == 200, body
    assert len(body["jobs"]) == 1
    item = body["jobs"][0]
    assert item["payload"]["chat_id"] == int(guest.external_id)
    assert set(item["payload"]) == {"chat_id", "text"}
    assert promo.code in item["payload"]["text"]
    assert voucher.code in item["payload"]["text"]
    assert "Напиток дня" in item["payload"]["text"]


async def test_outbox_ack_success_marks_job_done(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T13: ack success от того же worker_id переводит telegram_send в done."""
    await _approved_rewards_job(uow_factory, sync_engine, 9002)
    status, body = await _claim(api_client)
    assert status == 200, body
    job_id = body["jobs"][0]["id"]

    status, body = await _post(
        api_client, ACK, {"job_id": job_id, "worker_id": WORKER, "result": "success"}
    )

    assert status == 200, body
    assert body == {"status": "ack"}
    rows = _job_state(sync_engine, "telegram_send")
    assert rows[0].status == "done"


async def test_outbox_requires_hmac(api_client: AsyncClient, clean_db: None) -> None:
    resp = await api_client.post(
        CLAIM,
        content=b'{"worker_id":"tg-test:1","limit":10,"visibility_timeout_s":90}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_outbox_ack_user_blocked_unlinks_channel(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Блок бота (403 у Telegram): задача закрыта как терминальная, канал помечен unlinked_at."""
    guest, _ = await _approved_rewards_job(uow_factory, sync_engine, 9003)
    status, body = await _claim(api_client)
    assert status == 200, body
    job_id = body["jobs"][0]["id"]

    status, body = await _post(
        api_client,
        ACK,
        {
            "job_id": job_id,
            "worker_id": WORKER,
            "result": "terminal_business",
            "error": "user_blocked",
        },
    )

    assert status == 200, body
    rows = _job_state(sync_engine, "telegram_send")
    assert rows[0].status == "done"
    assert rows[0].last_error == "terminal_business_error:user_blocked"
    assert channel_unlinked(sync_engine, guest) is True


async def test_outbox_ack_unknown_job_is_404(api_client: AsyncClient, clean_db: None) -> None:
    status, body = await _post(
        api_client, ACK, {"job_id": 987654, "worker_id": WORKER, "result": "success"}
    )
    assert status == 404, body


async def test_ack_from_wrong_worker_fails_fencing(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """ТОП-3: задачу взял tg-test:1; ack от tg-other:2 -> 409 fencing_failed, статус locked."""
    await _approved_rewards_job(uow_factory, sync_engine, 9004)
    status, body = await _claim(api_client, worker=WORKER)
    assert status == 200, body
    job_id = body["jobs"][0]["id"]

    status, body = await _post(
        api_client, ACK, {"job_id": job_id, "worker_id": "tg-other:2", "result": "success"}
    )

    assert status == 409, body
    assert "fencing_failed" in json.dumps(body)
    rows = _job_state(sync_engine, "telegram_send")
    assert rows[0].status == "locked"
    assert rows[0].locked_by == WORKER


async def test_ack_retry_after_zero_is_accepted(
    api_client: AsyncClient, uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Ревью F: retry_after_s=0 принимается (мгновенный повтор)."""
    await _approved_rewards_job(uow_factory, sync_engine, 9005)
    status, body = await _claim(api_client)
    assert status == 200, body
    job_id = body["jobs"][0]["id"]

    status, body = await _post(
        api_client,
        ACK,
        {
            "job_id": job_id,
            "worker_id": WORKER,
            "result": "retryable",
            "error": "telegram_flood",
            "retry_after_s": 0,
        },
    )

    assert status == 200, body
    assert _job_state(sync_engine, "telegram_send")[0].status == "queued"
