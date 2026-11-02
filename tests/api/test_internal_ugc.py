"""HTTP-тесты фазы 3a: Бренд B через /internal/v1/activities (start, ugc/accept, ugc/resubmit).

Гость заводится тем же путём, что в tests/api/test_internal_activities.py: /start в боте,
затем согласие. Запросы подписываются HMAC так же, как у остальных внутренних эндпоинтов.
Ingest и решение модератора (фазы 3c и 3b) в этих тестах вызываются через ядро напрямую.
F2 - порядок commit и старта ответа; F3 - валидация URL; F7 - 403 для чужого прохождения.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import Engine

from app.core.activities.ugc import accept_from_user, ingest_file_done, reject, resubmit, start_ugc
from app.core.types.ids import AdminId, AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from tests.api.conftest import TEST_HMAC
from tests.conftest import ManualClock
from tests.factories import Guest, create_guest
from tests.factories.survey import UowFactory
from tests.factories.ugc import attempt_status, count_rows, create_admin, job_rows, ugc_rows

SLUG = "brand_b_photo_subscription"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
POST_URL = "https://www.instagram.com/p/example/"
PROFILE_URL = "https://www.instagram.com/example/"
TG_FILE_ID = "AgACAgIAAxkBAAIBHTTP"
RESUBMIT_FILE_ID = "AgACAgIAAxkBAAIBRESUBMIT"
MIME_JPEG = "image/jpeg"
SIZE_OK = 102400


def _auth(body: bytes) -> dict[str, str]:
    sig = hmac.new(TEST_HMAC.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Internal-Auth": sig, "Content-Type": "application/json"}


async def _post(
    api_client: AsyncClient, path: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    resp = await api_client.post(path, content=body, headers=_auth(body))
    return resp.status_code, resp.json()


async def _guest_with_consent(api_client: AsyncClient, external_id: int) -> None:
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
    status, body = await _post(
        api_client,
        "/internal/v1/consent/accept",
        {"channel_type": "telegram", "external_id": str(external_id)},
    )
    assert status == 200, body


async def _start_brand_b(api_client: AsyncClient, external_id: int) -> int:
    status, body = await _post(
        api_client,
        "/internal/v1/activities/start",
        {"channel_type": "telegram", "external_id": str(external_id), "activity_slug": SLUG},
    )
    assert status == 200, body
    return int(body["attempt_id"])


async def _http_accept(
    api_client: AsyncClient, external_id: int, attempt_id: int, tg_file_id: str = TG_FILE_ID
) -> int:
    status, body = await _post(
        api_client,
        "/internal/v1/activities/ugc/accept",
        {
            "channel_type": "telegram",
            "external_id": str(external_id),
            "attempt_id": attempt_id,
            "tg_file_id": tg_file_id,
            "mime_type": MIME_JPEG,
            "file_size_bytes": SIZE_OK,
            "post_url": POST_URL,
            "subscription_url": PROFILE_URL,
        },
    )
    assert status == 200, body
    return int(body["submission_id"])


async def _http_resubmit(
    api_client: AsyncClient, external_id: int, attempt_id: int, tg_file_id: str
) -> tuple[int, dict[str, Any]]:
    return await _post(
        api_client,
        "/internal/v1/activities/ugc/resubmit",
        {
            "channel_type": "telegram",
            "external_id": str(external_id),
            "attempt_id": attempt_id,
            "tg_file_id": tg_file_id,
            "mime_type": MIME_JPEG,
            "file_size_bytes": SIZE_OK,
            "post_url": POST_URL,
            "subscription_url": PROFILE_URL,
        },
    )


async def _core_ingest_done(uow_factory: UowFactory, submission_id: int) -> None:
    async with uow_factory() as uow:
        await ingest_file_done(
            uow.repositories.ugc,
            UgcSubmissionId(submission_id),
            "ugc",
            f"{uuid.uuid4()}.jpg",
            "image/jpeg",
            102400,
        )


async def _core_reject(uow_factory: UowFactory, submission_id: int, admin_id: int) -> None:
    async with uow_factory() as uow:
        r = uow.repositories
        await reject(
            r.ugc,
            r.attempts,
            r.catalog,
            r.jobs,
            UgcSubmissionId(submission_id),
            AdminId(admin_id),
            "low_quality",
            None,
            NOW,
        )


async def _rejected_first(
    api_client: AsyncClient, uow_factory: UowFactory, sync_engine: Engine, external_id: int
) -> tuple[int, int]:
    """Гость: consent, start, accept, ingest, reject. Возвращает (attempt_id, admin_id)."""
    await _guest_with_consent(api_client, external_id)
    attempt_id = await _start_brand_b(api_client, external_id)
    first = await _http_accept(api_client, external_id, attempt_id)
    await _core_ingest_done(uow_factory, first)
    admin_id = create_admin(sync_engine)
    await _core_reject(uow_factory, first, admin_id)
    return attempt_id, admin_id


async def test_start_brand_b_returns_ask_photo(api_client: AsyncClient) -> None:
    await _guest_with_consent(api_client, 2001)

    status, body = await _post(
        api_client,
        "/internal/v1/activities/start",
        {"channel_type": "telegram", "external_id": "2001", "activity_slug": SLUG},
    )

    assert status == 200, body
    assert body["kind"] == "ugc"
    assert body["next_step"] == {"kind": "ask_photo"}
    assert body["question"] is None


async def test_accept_without_photo_returns_422(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    await _guest_with_consent(api_client, 2002)
    attempt_id = await _start_brand_b(api_client, 2002)

    status, _ = await _post(
        api_client,
        "/internal/v1/activities/ugc/accept",
        {
            "channel_type": "telegram",
            "external_id": "2002",
            "attempt_id": attempt_id,
            "post_url": POST_URL,
        },
    )

    assert status == 422
    assert count_rows(sync_engine, "ugc_submissions") == 0


async def test_accept_happy_path(api_client: AsyncClient, sync_engine: Engine) -> None:
    await _guest_with_consent(api_client, 2003)
    attempt_id = await _start_brand_b(api_client, 2003)

    status, body = await _post(
        api_client,
        "/internal/v1/activities/ugc/accept",
        {
            "channel_type": "telegram",
            "external_id": "2003",
            "attempt_id": attempt_id,
            "tg_file_id": TG_FILE_ID,
            "mime_type": MIME_JPEG,
            "file_size_bytes": SIZE_OK,
            "post_url": POST_URL,
            "subscription_url": PROFILE_URL,
        },
    )

    assert status == 200, body
    assert body["status"] == "ok"
    assert isinstance(body["submission_id"], int)
    rows = ugc_rows(sync_engine, attempt_id)
    assert [row.status for row in rows] == ["receiving"]
    assert len(job_rows(sync_engine, "ingest_ugc_file")) == 1


async def test_accept_rejects_javascript_url(api_client: AsyncClient, sync_engine: Engine) -> None:
    """F3: схема строго https; javascript: и другие схемы - 422, до записи."""
    await _guest_with_consent(api_client, 2006)
    attempt_id = await _start_brand_b(api_client, 2006)

    status, _ = await _post(
        api_client,
        "/internal/v1/activities/ugc/accept",
        {
            "channel_type": "telegram",
            "external_id": "2006",
            "attempt_id": attempt_id,
            "tg_file_id": TG_FILE_ID,
            "mime_type": MIME_JPEG,
            "file_size_bytes": SIZE_OK,
            "post_url": "javascript:alert(1)",
        },
    )

    assert status == 422
    assert count_rows(sync_engine, "ugc_submissions") == 0


async def test_accept_from_other_user_returns_403(
    api_client: AsyncClient, sync_engine: Engine
) -> None:
    """F7: чужое прохождение - 403 attempt_not_owned, ничего не пишем."""
    await _guest_with_consent(api_client, 2007)
    attempt_id = await _start_brand_b(api_client, 2007)
    await _guest_with_consent(api_client, 2008)

    status, body = await _post(
        api_client,
        "/internal/v1/activities/ugc/accept",
        {
            "channel_type": "telegram",
            "external_id": "2008",
            "attempt_id": attempt_id,
            "tg_file_id": TG_FILE_ID,
            "mime_type": MIME_JPEG,
            "file_size_bytes": SIZE_OK,
            "post_url": POST_URL,
        },
    )

    assert status == 403
    assert body == {"error": "attempt_not_owned"}
    assert count_rows(sync_engine, "ugc_submissions") == 0


async def test_resubmit_success_returns_200(
    api_client: AsyncClient, sync_engine: Engine, uow_factory: UowFactory
) -> None:
    attempt_id, _ = await _rejected_first(api_client, uow_factory, sync_engine, 2004)

    status, body = await _http_resubmit(api_client, 2004, attempt_id, RESUBMIT_FILE_ID)

    assert status == 200, body
    assert body["status"] == "ok"
    assert isinstance(body["submission_id"], int)
    assert [row.status for row in ugc_rows(sync_engine, attempt_id)] == ["rejected", "receiving"]
    assert len(job_rows(sync_engine, "ingest_ugc_file")) == 2
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"


async def test_resubmit_limit_returns_409_and_closes_attempt(
    api_client: AsyncClient, sync_engine: Engine, uow_factory: UowFactory
) -> None:
    attempt_id, admin_id = await _rejected_first(api_client, uow_factory, sync_engine, 2005)
    status, body = await _http_resubmit(api_client, 2005, attempt_id, RESUBMIT_FILE_ID)
    assert status == 200, body
    second = int(body["submission_id"])
    await _core_ingest_done(uow_factory, second)
    await _core_reject(uow_factory, second, admin_id)

    status, body = await _http_resubmit(api_client, 2005, attempt_id, "third_file")

    assert status == 409
    assert body == {"error": "resubmit_limit", "attempt_closed": True}
    assert attempt_status(sync_engine, attempt_id) == "rejected"
    assert [row.status for row in ugc_rows(sync_engine, attempt_id)] == ["rejected", "rejected"]
    assert len(job_rows(sync_engine, "ingest_ugc_file")) == 2


async def test_resubmit_from_other_user_returns_403(
    api_client: AsyncClient, sync_engine: Engine, uow_factory: UowFactory
) -> None:
    """F7: повтор чужого прохождения - 403, строки не меняются."""
    attempt_id, _ = await _rejected_first(api_client, uow_factory, sync_engine, 2009)
    await _guest_with_consent(api_client, 2010)

    status, body = await _http_resubmit(api_client, 2010, attempt_id, RESUBMIT_FILE_ID)

    assert status == 403
    assert body == {"error": "attempt_not_owned"}
    assert [row.status for row in ugc_rows(sync_engine, attempt_id)] == ["rejected"]
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"


# ---------------------------------------------------------------- F2: порядок commit и ответа


@pytest_asyncio.fixture
async def ordered_client(
    pg_url: str, manual_clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, list[str]]]:
    """Клиент, фиксирующий порядок событий: commit UoW и старт HTTP-ответа (F2)."""
    from app.db.engine import create_engine_from_url
    from app.db.session import make_session_factory
    from app.db.unit_of_work import UnitOfWork
    from app.main import app

    events: list[str] = []
    original_commit = UnitOfWork.commit

    async def spy_commit(self: UnitOfWork) -> None:
        events.append("commit")
        await original_commit(self)

    monkeypatch.setattr(UnitOfWork, "commit", spy_commit)
    engine = create_engine_from_url(pg_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.clock = manual_clock

    async def recording_app(scope: Any, receive: Any, send: Any) -> None:
        async def send_with_marks(message: Any) -> None:
            if message["type"] == "http.response.start":
                events.append("response_start")
            await send(message)

        await app(scope, receive, send_with_marks)

    transport = ASGITransport(app=recording_app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, events
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def _core_accept(uow_factory: UowFactory, guest: Guest, attempt_id: int) -> int:
    async with uow_factory() as uow:
        accepted = await accept_from_user(
            uow.repositories.consents,
            uow.repositories.catalog,
            uow.repositories.attempts,
            uow.repositories.ugc,
            uow.repositories.jobs,
            UserId(guest.user_id),
            AttemptId(attempt_id),
            TG_FILE_ID,
            MIME_JPEG,
            SIZE_OK,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    return int(accepted.submission_id)


async def _core_resubmit(
    uow_factory: UowFactory, guest: Guest, attempt_id: int, tg_file_id: str
) -> int:
    async with uow_factory() as uow:
        result = await resubmit(
            uow.repositories.consents,
            uow.repositories.catalog,
            uow.repositories.attempts,
            uow.repositories.ugc,
            uow.repositories.jobs,
            UserId(guest.user_id),
            AttemptId(attempt_id),
            tg_file_id,
            MIME_JPEG,
            SIZE_OK,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    assert result.submission_id is not None
    return int(result.submission_id)


async def test_409_arrives_after_commit(
    ordered_client: tuple[AsyncClient, list[str]],
    sync_engine: Engine,
    uow_factory: UowFactory,
) -> None:
    """F2: 409 по лимиту уходит клиенту после commit закрытия прохождения."""
    client, events = ordered_client
    guest: Guest = create_guest(sync_engine, external_id="2030")
    async with uow_factory() as uow:
        started = await start_ugc(
            uow.repositories.consents,
            uow.repositories.catalog,
            uow.repositories.attempts,
            UserId(guest.user_id),
            SLUG,
            ChannelAccountId(guest.channel_account_id),
            NOW,
        )
    attempt_id = int(started.attempt_id)
    admin_id = create_admin(sync_engine)
    first = await _core_accept(uow_factory, guest, attempt_id)
    await _core_ingest_done(uow_factory, first)
    await _core_reject(uow_factory, first, admin_id)
    second = await _core_resubmit(uow_factory, guest, attempt_id, "second_file")
    await _core_ingest_done(uow_factory, second)
    await _core_reject(uow_factory, second, admin_id)

    events.clear()
    status, body = await _http_resubmit(client, 2030, attempt_id, "third_file")

    assert status == 409
    assert body == {"error": "resubmit_limit", "attempt_closed": True}
    assert "commit" in events
    assert "response_start" in events
    assert events.index("commit") < events.index("response_start")
    assert attempt_status(sync_engine, attempt_id) == "rejected"
    assert count_rows(sync_engine, "ugc_submissions") == 2
