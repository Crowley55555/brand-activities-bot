"""Маршрут GET /media/frame/{id}?t= (B4, ADR-40): байты результата Polaroid по подписи кадра.

Подпись кадра - отдельная соль (ugc-media-token-v1 у фото подачи). Токен подачи с тем же id
маршрут кадра не принимает: ссылка на исходное фото не открывает результат рамки.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.adapters.object_storage.local_fs import LocalFsObjectStorage
from app.core.media import sign_frame_media_token, sign_media_token
from tests.api.conftest import TEST_HMAC
from tests.factories import create_attempt, create_guest
from tests.factories.brand_d import PHOTO_LIMITS, make_activity

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FRAMES_BUCKET = "frames-test"
RESULT = b"\xff\xd8\xff\xe0frame-result\xff\xd9"
RESULT_KEY = "frames/test/result.jpg"


@pytest.fixture
def storage(tmp_path: Path, api_client: AsyncClient) -> LocalFsObjectStorage:
    from app.main import app

    local = LocalFsObjectStorage(tmp_path, signing_key=TEST_HMAC)
    app.state.object_storage = local
    return local


def _done_frame(engine: Engine, attempt_id: int) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO frame_submissions (attempt_id, source_bucket, source_key, "
                    "source_content_type, source_size_bytes, result_bucket, result_key, "
                    "result_content_type, result_size_bytes, status, completed_at, "
                    "created_at, updated_at) VALUES (:aid, 'ugc-test', 'src/1.jpg', 'image/jpeg', "
                    "100, :rb, :rk, 'image/jpeg', :size, 'done', now(), now(), now()) RETURNING id"
                ),
                {
                    "aid": attempt_id,
                    "rb": FRAMES_BUCKET,
                    "rk": RESULT_KEY,
                    "size": len(RESULT),
                },
            ).scalar_one()
        )


async def test_frame_media_returns_jpeg_with_signed_token(
    api_client: AsyncClient, sync_engine: Engine, storage: LocalFsObjectStorage
) -> None:
    guest = create_guest(sync_engine, external_id="3401", now=NOW)
    activity = make_activity(sync_engine, "photo_frame", PHOTO_LIMITS)
    attempt_id = create_attempt(sync_engine, guest, activity.slug, status="completed")
    frame_id = _done_frame(sync_engine, attempt_id)
    await storage.put(FRAMES_BUCKET, RESULT_KEY, RESULT, "image/jpeg")

    ticket = sign_frame_media_token(TEST_HMAC, frame_id)
    response = await api_client.get(f"/media/frame/{frame_id}", params={"t": ticket})

    assert response.status_code == 200
    assert response.content == RESULT
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_frame_media_rejects_ugc_token_and_missing_token(
    api_client: AsyncClient, sync_engine: Engine, storage: LocalFsObjectStorage
) -> None:
    guest = create_guest(sync_engine, external_id="3402", now=NOW)
    activity = make_activity(sync_engine, "photo_frame", PHOTO_LIMITS)
    attempt_id = create_attempt(sync_engine, guest, activity.slug, status="completed")
    frame_id = _done_frame(sync_engine, attempt_id)
    await storage.put(FRAMES_BUCKET, RESULT_KEY, RESULT, "image/jpeg")

    ugc_ticket = sign_media_token(TEST_HMAC, frame_id)
    with_ugc = await api_client.get(f"/media/frame/{frame_id}", params={"t": ugc_ticket})
    without = await api_client.get(f"/media/frame/{frame_id}")

    assert with_ugc.status_code == 403
    assert without.status_code == 403
