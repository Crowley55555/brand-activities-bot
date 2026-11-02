"""Фаза 3c, п.13 ревью: /media при OBJECT_STORAGE_IMPL=selectel_s3 отвечает 302 на presigned_get.

Токен проверяется до выдачи ссылки (как в 3b); без токена - 403. Бакет и ключ наружу не уходят,
кроме как внутри подписанного Location на TTL.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.core.media import preview_url
from app.main import app
from tests.api.conftest import TEST_HMAC
from tests.factories.workers import ingest_done, brand_b_submission


class _PresignStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:
        self.calls.append((bucket, key, ttl_seconds))
        return f"https://storage.test/{bucket}/{key}?X-Amz-Signature=abc"


@pytest.fixture
def s3_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OBJECT_STORAGE_IMPL", "selectel_s3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_media_s3_mode_redirects_to_presigned_url(
    api_client: AsyncClient,
    uow_factory: Any,
    clean_db: None,
    sync_engine: Any,
    s3_mode: None,
) -> None:
    case = await brand_b_submission(uow_factory, sync_engine, 9301)
    await ingest_done(uow_factory, case.submission_id)
    storage = _PresignStorage()
    app.state.object_storage = storage

    resp = await api_client.get(preview_url(TEST_HMAC, case.submission_id), follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"https://storage.test/ugc/{case.submission_id}/")
    assert storage.calls
    assert storage.calls[0][2] == 300

    unsigned = await api_client.get(f"/media/{case.submission_id}", follow_redirects=False)
    assert unsigned.status_code == 403
