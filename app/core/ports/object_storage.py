"""Порт объектного хранилища (ADR-15, 06 §2, правило 4): UGC-файлы и результаты рамки.

Ядро знает только этот интерфейс. Реализации: `app/adapters/object_storage/local_fs.py`
(dev, фаза 3a) и S3-адаптер (клиент S3 из адаптера, фаза 3c). Ядро не импортирует клиенты
облачного хранилища и обработку изображений (правило 3).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ObjectStorageUnavailable(Exception):
    """Хранилище временно недоступно (5xx, 429, сеть): ingest и s3_delete повторят задачу."""


@runtime_checkable
class ObjectStoragePort(Protocol):
    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        """Сохранить объект. Возвращает адрес объекта в хранилище, не публичную ссылку."""
        ...

    async def get(self, bucket: str, key: str) -> bytes:
        """Прочитать объект. Его нет - NotFound('object_not_found')."""
        ...

    async def delete(self, bucket: str, key: str) -> None:
        """Удалить объект. Повтор безопасен: отсутствующий объект - не ошибка (04 §7)."""
        ...

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:
        """Ссылка на чтение с коротким сроком жизни. Гостю отдаём только `/media/<id>`."""
        ...
