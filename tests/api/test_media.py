"""Фаза 3b, П2: GET /media/{submission_id}?t=<token> отдаёт байты фото UGC с кодом 200.

Токен подписывает core.media (itsdangerous, INTERNAL_API_AUTH_SECRET, своя соль) и живёт не дольше
UGC_PREVIEW_TTL_SECONDS. Без токена, с чужим или истёкшим токеном - 403 media_token_invalid.
Сессия админки не нужна: маршрут вне /admin, защита - подпись и срок. В фазе 3c тот же маршрут
будет отвечать 302 на presigned_get реального S3.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy.engine import Engine

from app.adapters.object_storage.local_fs import LocalFsObjectStorage
from app.config import get_settings
from app.core.activities.ugc import accept_from_user, ingest_file_done, start_ugc
from app.core.media import preview_url, sign_media_token
from app.core.types.ids import AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from tests.api.conftest import TEST_HMAC
from tests.factories import create_guest
from tests.factories.survey import UowFactory

SLUG = "brand_b_photo_subscription"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
TG_FILE_ID = "AgACAgIAAxkBAAIBMEDIAFILE"
POST_URL = "https://www.instagram.com/p/example/"
PROFILE_URL = "https://www.instagram.com/example/"
# Заголовок JPEG (SOI + APP0 JFIF) и конец файла: libmagic узнаёт image/jpeg по сигнатуре.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


@pytest.fixture
def object_storage(tmp_path: Path, api_client: AsyncClient) -> LocalFsObjectStorage:
    """Хранилище фото для маршрута /media (lifespan в тестах не идёт, кладём в state)."""
    from app.main import app

    storage = LocalFsObjectStorage(tmp_path, signing_key=TEST_HMAC)
    app.state.object_storage = storage
    return storage


async def _pending_photo(
    uow_factory: UowFactory,
    storage: LocalFsObjectStorage,
    engine: Engine,
    external_id: str,
) -> int:
    """Гость, прохождение, фото, файл в хранилище, pending. Возвращает id подачи."""
    guest = create_guest(engine, external_id)
    async with uow_factory() as uow:
        r = uow.repositories
        started = await start_ugc(
            r.consents,
            r.catalog,
            r.attempts,
            UserId(guest.user_id),
            SLUG,
            ChannelAccountId(guest.channel_account_id),
            NOW,
        )
    attempt_id = int(started.attempt_id)
    async with uow_factory() as uow:
        r = uow.repositories
        accepted = await accept_from_user(
            r.consents,
            r.catalog,
            r.attempts,
            r.ugc,
            r.jobs,
            UserId(guest.user_id),
            AttemptId(attempt_id),
            TG_FILE_ID,
            "image/jpeg",
            len(JPEG),
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    submission_id = int(accepted.submission_id)
    key = f"{uuid.uuid4()}.jpg"
    await storage.put("ugc", key, JPEG, "image/jpeg")
    async with uow_factory() as uow:
        await ingest_file_done(
            uow.repositories.ugc,
            UgcSubmissionId(submission_id),
            "ugc",
            key,
            "image/jpeg",
            len(JPEG),
        )
    return submission_id


async def test_media_without_token_returns_403(
    api_client: AsyncClient,
    uow_factory: UowFactory,
    sync_engine: Engine,
    object_storage: LocalFsObjectStorage,
) -> None:
    submission_id = await _pending_photo(uow_factory, object_storage, sync_engine, "4001")

    resp = await api_client.get(f"/media/{submission_id}")

    assert resp.status_code == 403
    assert resp.json() == {"error": "media_token_invalid"}


async def test_media_with_valid_token_returns_bytes(
    api_client: AsyncClient,
    uow_factory: UowFactory,
    sync_engine: Engine,
    object_storage: LocalFsObjectStorage,
) -> None:
    """Ссылка из core.media.preview_url (то, что видит модератор) отдаёт байты фото."""
    submission_id = await _pending_photo(uow_factory, object_storage, sync_engine, "4002")
    url = preview_url(TEST_HMAC, submission_id)

    resp = await api_client.get(url)

    assert resp.status_code == 200
    assert resp.content == JPEG
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_media_with_expired_token_returns_403(
    api_client: AsyncClient,
    uow_factory: UowFactory,
    sync_engine: Engine,
    object_storage: LocalFsObjectStorage,
) -> None:
    """Токен старше UGC_PREVIEW_TTL_SECONDS не проходит проверку срока."""
    submission_id = await _pending_photo(uow_factory, object_storage, sync_engine, "4003")
    ttl = get_settings().ugc_preview_ttl_seconds
    issued_long_ago = datetime.now(UTC) - timedelta(seconds=2 * ttl)
    with freeze_time(issued_long_ago):
        signed_ticket = sign_media_token(TEST_HMAC, submission_id)

    resp = await api_client.get(f"/media/{submission_id}", params={"t": signed_ticket})

    assert resp.status_code == 403
    assert resp.json() == {"error": "media_token_invalid"}


async def test_media_token_is_bound_to_its_submission(
    api_client: AsyncClient,
    uow_factory: UowFactory,
    sync_engine: Engine,
    object_storage: LocalFsObjectStorage,
) -> None:
    """Токен одной подачи не открывает фото другой: подпись связана с submission_id."""
    first = await _pending_photo(uow_factory, object_storage, sync_engine, "4004")
    second = await _pending_photo(uow_factory, object_storage, sync_engine, "4005")
    token_for_first = sign_media_token(TEST_HMAC, first)

    resp = await api_client.get(f"/media/{second}", params={"t": token_for_first})

    assert resp.status_code == 403
    assert resp.json() == {"error": "media_token_invalid"}
