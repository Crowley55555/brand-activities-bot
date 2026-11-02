"""Адаптер объектного хранилища Selectel и MinIO через S3 API (ADR-29, фаза 3c).

I/O (put, get, delete) - aioboto3-клиент (async). presigned_get - синхронный boto3-клиент: подпись
считается локально, сети нет, поэтому синхронный вызов допустим и в async-коде (п.12 ревью).
Один файл обслуживает и Selectel, и MinIO: отличается только S3_ENDPOINT_URL. Отдельного
minio_dev.py нет (решение координатора, п.12). Ошибки S3 переводятся в порт: отсутствие объекта -
NotFound, остальное - ObjectStorageUnavailable (повтор задачи).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import aioboto3
import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.common.errors import NotFound
from app.config import Settings
from app.core.ports.object_storage import ObjectStorageUnavailable

ClientFactory = Callable[[], AbstractAsyncContextManager[Any]]
_MISSING_CODES: frozenset[str] = frozenset({"NoSuchKey", "NotFound", "404"})


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _MISSING_CODES:
            return NotFound("object_not_found")
    return ObjectStorageUnavailable()


class SelectelS3Storage:
    """Реализация ObjectStoragePort поверх S3 API."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory,
        presign_client: Any,
        endpoint_url: str,
    ) -> None:
        self._client_factory = client_factory
        self._presign = presign_client
        self._endpoint = endpoint_url.rstrip("/")

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        try:
            async with self._client_factory() as s3:
                await s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        except (ClientError, BotoCoreError) as exc:
            raise _translate(exc) from exc
        return f"{self._endpoint}/{bucket}/{key}"

    async def get(self, bucket: str, key: str) -> bytes:
        try:
            async with self._client_factory() as s3:
                response = await s3.get_object(Bucket=bucket, Key=key)
                async with response["Body"] as stream:
                    return bytes(await stream.read())
        except (ClientError, BotoCoreError) as exc:
            raise _translate(exc) from exc

    async def delete(self, bucket: str, key: str) -> None:
        try:
            async with self._client_factory() as s3:
                await s3.delete_object(Bucket=bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise _translate(exc) from exc

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:
        url = self._presign.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
        return str(url)


def build_selectel_s3(settings: Settings) -> SelectelS3Storage:
    """Сборка из настроек. Пустые endpoint или ключи - ошибка запуска, не тихий откат."""
    filled = (settings.s3_endpoint_url, settings.s3_access_key_id, settings.s3_secret_access_key)
    if not all(filled):
        raise RuntimeError("s3_settings_missing")
    endpoint = settings.s3_endpoint_url
    s3_cred = settings.s3_secret_access_key
    access_val = settings.s3_access_key_id
    region = settings.s3_region_name
    session = aioboto3.Session(
        aws_access_key_id=access_val,
        aws_secret_access_key=s3_cred,
        region_name=region,
    )
    presign = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_val,
        aws_secret_access_key=s3_cred,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )

    def factory() -> AbstractAsyncContextManager[Any]:
        return session.client("s3", endpoint_url=endpoint)

    return SelectelS3Storage(client_factory=factory, presign_client=presign, endpoint_url=endpoint)
