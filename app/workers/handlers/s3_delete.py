"""Handler s3_delete (ADR-28, канон 04 §3, строка 23 таблицы): удаление объекта из хранилища.

Повтор безопасен: удаление отсутствующего объекта для S3 - не ошибка (канон 04 §4, строка 38).
"""

from __future__ import annotations

from app.core.jobs.types import JobRecord, RetryableJobError
from app.core.ports.object_storage import ObjectStorageUnavailable
from app.db.unit_of_work import UnitOfWork
from app.workers.context import WorkerDeps


async def run(job: JobRecord, uow: UnitOfWork, deps: WorkerDeps) -> None:
    bucket = str(job.payload["bucket"])
    key = str(job.payload["key"])
    try:
        await deps.storage.delete(bucket, key)
    except ObjectStorageUnavailable as exc:
        raise RetryableJobError("storage_unavailable") from exc
