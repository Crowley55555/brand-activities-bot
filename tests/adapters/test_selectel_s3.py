"""Адаптер S3 (ADR-29): I/O через aioboto3-клиент, presign - синхронный boto3 (без сети).

Клиенты подменяются AsyncMock; реальные вызовы S3 в тестах не делаются (moto не ставим).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from app.adapters.object_storage.selectel_s3 import SelectelS3Storage
from app.core.ports.object_storage import ObjectStorageUnavailable


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _Body:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def read(self) -> bytes:
        return self._data


class _ClientContext:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _s3_client(data: bytes) -> Any:
    client = MagicMock()
    client.put_object = AsyncMock(return_value={})
    client.get_object = AsyncMock(return_value={"Body": _Body(data)})
    client.delete_object = AsyncMock(return_value={})
    return client


def _storage(client: Any, presign: Any) -> SelectelS3Storage:
    return SelectelS3Storage(
        client_factory=lambda: _ClientContext(client),
        presign_client=presign,
        endpoint_url="https://s3.example.test",
    )


async def test_s3_adapter_round_trip() -> None:
    """T10: put/get/delete через клиент: параметры вызовов и возврат байтов."""
    client = _s3_client(b"photo-bytes")
    storage = _storage(client, MagicMock())

    url = await storage.put("ugc", "1/abc.jpg", b"photo-bytes", "image/jpeg")
    assert url == "https://s3.example.test/ugc/1/abc.jpg"
    client.put_object.assert_awaited_once_with(
        Bucket="ugc", Key="1/abc.jpg", Body=b"photo-bytes", ContentType="image/jpeg"
    )

    data = await storage.get("ugc", "1/abc.jpg")
    assert data == b"photo-bytes"
    client.get_object.assert_awaited_once_with(Bucket="ugc", Key="1/abc.jpg")

    await storage.delete("ugc", "1/abc.jpg")
    client.delete_object.assert_awaited_once_with(Bucket="ugc", Key="1/abc.jpg")


def test_s3_presigned_get_returns_valid_url() -> None:
    """T11: presigned_get отдаёт URL подписи; параметры Params/ExpiresIn переданы в boto3."""
    presign = MagicMock()
    presign.generate_presigned_url.return_value = (
        "https://s3.example.test/ugc/1/abc.jpg?X-Amz-Expires=300&X-Amz-Signature=abc123"
    )
    storage = _storage(_s3_client(b""), presign)

    url = storage.presigned_get("ugc", "1/abc.jpg", 300)

    assert url.startswith("https://s3.example.test/ugc/1/abc.jpg?")
    assert "X-Amz-Signature=" in url
    presign.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "ugc", "Key": "1/abc.jpg"},
        ExpiresIn=300,
    )


async def test_s3_server_error_maps_to_unavailable() -> None:
    client = _s3_client(b"")
    client.get_object = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "InternalError"}}, "GetObject")
    )
    storage = _storage(client, MagicMock())
    with pytest.raises(ObjectStorageUnavailable):
        await storage.get("ugc", "1/abc.jpg")
