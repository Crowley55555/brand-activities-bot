"""Dev-адаптер объектного хранилища: локальная папка (ADR-15, фаза 3a).

Файлы лежат в `<root>/<bucket>/<key>`, где root - UGC_LOCAL_PATH (по умолчанию ./data/ugc;
каталог закрыт .gitignore). Это MVP-отход от канона 05 §2.7 (MinIO в dev), зафиксирован в
журнале 05. S3-адаптер - фаза 3c.

`presigned_get` и `verify_presigned` - контракт под S3-адаптер фазы 3c (302 на presigned URL).
В фазе 3b ссылки на фото выдаёт и проверяет core.media (`core/media/tokens.py`, П2): маршрут
/media/<submission_id>?t=<token> отдаёт байты сам, и эти два метода в 3b не вызываются. Ключ
подписи берётся из INTERNAL_API_AUTH_SECRET (MVP); в 3c - отдельный MEDIA_SIGNING_KEY по решению
оркестратора.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from itsdangerous import BadData, URLSafeSerializer

from app.common.errors import NotFound, ValidationError

_BUCKET_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}")
_SALT: Final[str] = "ugc-media-link"
MAX_TTL_SECONDS: Final[int] = 3600


def _encode_target(bucket: str, key: str) -> str:
    raw = json.dumps([bucket, key], ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_target(target: str) -> tuple[str, str]:
    padded = target + "=" * (-len(target) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error) as exc:
        raise ValidationError("media_link_invalid") from exc
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)):
        raise ValidationError("media_link_invalid")
    return value[0], value[1]


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _unlink_if_exists(path: Path) -> None:
    path.unlink(missing_ok=True)


class LocalFsObjectStorage:
    """Реализация ObjectStoragePort поверх файловой системы."""

    def __init__(
        self,
        root: Path,
        signing_key: str,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._root = root
        self._signing_key = signing_key
        self._now = now_fn

    def _path(self, bucket: str, key: str) -> Path:
        """Путь внутри root; всё, что ведёт наружу (`..`, абсолютные пути), отбрасываем."""
        if not _BUCKET_RE.fullmatch(bucket):
            raise ValidationError("object_bucket_invalid")
        if not _KEY_RE.fullmatch(key) or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValidationError("object_key_invalid")
        base = (self._root / bucket).resolve()
        path = (base / key).resolve()
        if base not in path.parents:
            raise ValidationError("object_key_invalid")
        return path

    def _serializer(self) -> URLSafeSerializer:
        if not self._signing_key:
            # Fail-closed: без ключа ссылки не выдаём и не принимаем.
            raise RuntimeError("media_signing_key_missing")
        return URLSafeSerializer(self._signing_key, salt=_SALT)

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        # content_type в dev не хранится: тип файла живёт в ugc_submissions.file_content_type.
        path = self._path(bucket, key)
        await asyncio.to_thread(_write_atomic, path, data)
        return f"local://{bucket}/{key}"

    async def get(self, bucket: str, key: str) -> bytes:
        path = self._path(bucket, key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise NotFound("object_not_found") from exc

    async def delete(self, bucket: str, key: str) -> None:
        path = self._path(bucket, key)
        await asyncio.to_thread(_unlink_if_exists, path)

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:
        self._path(bucket, key)  # валидация адреса без обращения к диску
        if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValidationError("media_ttl_invalid")
        target = _encode_target(bucket, key)
        exp = int(self._now()) + ttl_seconds
        signed = self._serializer().dumps({"id": target, "exp": exp})
        return f"/media/{target}?t={signed}"

    def verify_presigned(self, url: str) -> tuple[str, str]:
        """Проверить ссылку от `presigned_get`: подпись, id и срок. Возвращает (bucket, key)."""
        path_part, _, query = url.partition("?")
        if not path_part.startswith("/media/") or not query.startswith("t="):
            raise ValidationError("media_link_invalid")
        target = path_part.removeprefix("/media/")
        signed = query.removeprefix("t=")
        try:
            payload: Any = self._serializer().loads(signed)
        except BadData as exc:
            raise ValidationError("media_link_invalid") from exc
        if not isinstance(payload, dict) or payload.get("id") != target:
            raise ValidationError("media_link_invalid")
        exp = payload.get("exp")
        if not isinstance(exp, int) or isinstance(exp, bool):
            raise ValidationError("media_link_invalid")
        if int(self._now()) > exp:
            raise ValidationError("media_link_expired")
        bucket, key = _decode_target(target)
        self._path(bucket, key)
        return bucket, key
