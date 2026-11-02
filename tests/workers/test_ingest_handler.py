"""Handler ingest_ugc_file (ADR-30): consent-first (F6), Telegram через порт, MIME, S3, dead-хук.

T5-T7 - приёмочные из промта (п.7 ревью координатора: T5 -> ingest_failed, без уведомления).
ТОП-1: сбои приёмки уходят в notify_ingest_failed с системной причиной и текстом ingest_failed,
а не в notify_ugc_rejected (отказ модератора).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.jobs.outbox import OutboxTexts, claim_outbox
from app.core.ports.file_sniffer import MimeInfo
from app.core.ports.object_storage import ObjectStorageUnavailable
from app.core.ports.telegram_files import (
    TelegramFileNotFound,
    TelegramFileTooLarge,
    TelegramFileTransient,
)
from app.workers.context import WorkerDeps
from app.workers.runner import WorkerRunner
from tests.conftest import ManualClock
from tests.factories import revoke_consent
from tests.factories.workers import (
    NOW,
    jobs_of_kind,
    brand_b_submission,
    submission_status,
)

BUCKET = "ugc-test"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 8
INGEST_TEXTS = OutboxTexts(
    reward_notice="Готово! Промокод: {promocode}. Напиток: {voucher_title}. Код: {voucher_code}",
    ugc_rejected_retry="Ваша работа отклонена: {reason}. Пришлите другое фото через /start.",
    ugc_rejected_closed="Ваша работа отклонена: {reason}. К сожалению, попытки исчерпаны.",
    ingest_failed_retry="Ваш файл не удалось принять: {reason}. Попробуйте ещё раз через /start.",
    ingest_failed_closed="Технически приёмка завершилась неудачно, обратитесь к администратору.",
)


class FakeFiles:
    def __init__(self, result: tuple[bytes, str] | None = None, exc: Exception | None = None):
        self._result = result or (JPEG, "image/jpeg")
        self._exc = exc
        self.calls = 0

    async def fetch(self, tg_file_id: str) -> tuple[bytes, str]:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


class FakeStorage:
    def __init__(self, exc: Exception | None = None) -> None:
        self.puts: list[tuple[str, str, bytes, str]] = []
        self._exc = exc

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        if self._exc is not None:
            raise self._exc
        self.puts.append((bucket, key, data, content_type))
        return f"https://storage.test/{bucket}/{key}"

    async def get(self, bucket: str, key: str) -> bytes:  # pragma: no cover - не нужен
        raise AssertionError("get не вызывается в ingest")

    async def delete(self, bucket: str, key: str) -> None:  # pragma: no cover
        raise AssertionError("delete не вызывается в ingest")

    def presigned_get(self, bucket: str, key: str, ttl_seconds: int) -> str:  # pragma: no cover
        raise AssertionError("presigned_get не вызывается в ingest")


class FakeSniffer:
    def __init__(self, mime: str, is_image: bool = True) -> None:
        self._info = MimeInfo(mime=mime, is_image=is_image)

    def sniff(self, data: bytes) -> MimeInfo:
        return self._info


def _deps(files: Any, storage: Any, sniffer: Any) -> WorkerDeps:
    return WorkerDeps(
        files=files,
        storage=storage,
        sniffer=sniffer,
        clock=ManualClock(NOW),
        ugc_bucket=BUCKET,
    )


async def _run_ingest(uow_factory: Any, deps: WorkerDeps) -> int:
    runner = WorkerRunner(
        uow_factory, deps, kinds=("ingest_ugc_file",), worker_id="t-ingest", poll_interval_s=0.01
    )
    return await runner.run_once()


async def _run_notify(uow_factory: Any, deps: WorkerDeps) -> int:
    runner = WorkerRunner(
        uow_factory,
        deps,
        kinds=("notify_ingest_failed",),
        worker_id="t-notify-sys",
        poll_interval_s=0.01,
    )
    return await runner.run_once()


async def test_ingest_handler_consent_revoked_marks_ingest_failed(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T5: отзыв согласия - Telegram не трогаем, подача ingest_failed, без уведомления."""
    case = await brand_b_submission(uow_factory, sync_engine, 7001)
    revoke_consent(sync_engine, case.guest)
    files, storage = FakeFiles(), FakeStorage()

    processed = await _run_ingest(uow_factory, _deps(files, storage, FakeSniffer("image/jpeg")))

    assert processed == 1
    assert files.calls == 0
    assert storage.puts == []
    assert submission_status(sync_engine, case.submission_id) == "ingest_failed"
    job = jobs_of_kind(sync_engine, "ingest_ugc_file")[0]
    assert job.status == "done"
    assert job.last_error == "terminal_business_error:consent_revoked"
    assert jobs_of_kind(sync_engine, "notify_ingest_failed") == []
    assert jobs_of_kind(sync_engine, "notify_ugc_rejected") == []


async def test_ingest_handler_mime_mismatch_marks_failed(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T6: байты не совпали с allowed_mimes - ingest_failed, системное уведомление, терминал."""
    case = await brand_b_submission(uow_factory, sync_engine, 7002)
    files = FakeFiles(result=(b"%PDF-1.4 body", "application/pdf"))
    storage = FakeStorage()

    await _run_ingest(
        uow_factory, _deps(files, storage, FakeSniffer("application/pdf", is_image=False))
    )

    assert storage.puts == []
    assert submission_status(sync_engine, case.submission_id) == "ingest_failed"
    job = jobs_of_kind(sync_engine, "ingest_ugc_file")[0]
    assert job.status == "done"
    assert job.last_error == "terminal_business_error:mime_mismatch"
    notify = jobs_of_kind(sync_engine, "notify_ingest_failed")
    assert len(notify) == 1
    assert notify[0].payload["system_reason_slug"] == "mime_mismatch"
    assert notify[0].payload["attempt_id"] == case.attempt_id
    assert notify[0].payload["attempt_closed"] is False
    assert jobs_of_kind(sync_engine, "notify_ugc_rejected") == []


async def test_ingest_mime_mismatch_enqueues_system_notify_with_text(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """ТОП-1: mime_mismatch -> notify_ingest_failed -> telegram_send(ingest_failed) -> текст.

    Текст - техническая неудача с подписью из system_reject_reasons, а не «работа отклонена».
    """
    case = await brand_b_submission(uow_factory, sync_engine, 7009)
    files = FakeFiles(result=(b"%PDF-1.4 body", "application/pdf"))
    deps = _deps(files, FakeStorage(), FakeSniffer("application/pdf", is_image=False))
    await _run_ingest(uow_factory, deps)

    assert await _run_notify(uow_factory, deps) == 1

    sends = jobs_of_kind(sync_engine, "telegram_send")
    assert len(sends) == 1
    assert sends[0].payload["template"] == "ingest_failed"
    assert sends[0].payload["context"]["reason_slug"] == "mime_mismatch"
    async with uow_factory() as uow:
        messages = await claim_outbox(
            uow.repositories.jobs,
            uow.repositories.rewards,
            uow.repositories.catalog,
            INGEST_TEXTS,
            worker_id="t-render",
            limit=10,
            visibility_timeout_s=90,
        )
    assert len(messages) == 1
    body = messages[0].text
    assert "Формат файла не поддерживается" in body
    assert "Попробуйте ещё раз через /start" in body
    assert "Ваша работа отклонена" not in body
    assert case.submission_id


async def test_ingest_handler_happy_path(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T7: Telegram -> sniff -> S3 put -> pending с bucket, key, размером и типом."""
    case = await brand_b_submission(uow_factory, sync_engine, 7003)
    files, storage = FakeFiles(), FakeStorage()

    await _run_ingest(uow_factory, _deps(files, storage, FakeSniffer("image/jpeg")))

    assert submission_status(sync_engine, case.submission_id) == "pending"
    assert len(storage.puts) == 1
    bucket, key, data, content_type = storage.puts[0]
    assert bucket == BUCKET
    assert key.startswith(f"{case.submission_id}/")
    assert key.endswith(".jpg")
    assert data == JPEG
    assert content_type == "image/jpeg"
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT file_bucket, file_key, file_content_type, file_size_bytes "
                "FROM ugc_submissions WHERE id = :id"
            ),
            {"id": case.submission_id},
        ).one()
    assert row.file_bucket == BUCKET
    assert row.file_key == key
    assert row.file_content_type == "image/jpeg"
    assert row.file_size_bytes == len(JPEG)
    assert jobs_of_kind(sync_engine, "ingest_ugc_file")[0].status == "done"


async def test_ingest_file_not_found_enqueues_system_notify(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """404 Telegram: ingest_failed + notify_ingest_failed(file_not_found), задача терминальная."""
    case = await brand_b_submission(uow_factory, sync_engine, 7004)
    files = FakeFiles(exc=TelegramFileNotFound())

    await _run_ingest(uow_factory, _deps(files, FakeStorage(), FakeSniffer("image/jpeg")))

    assert submission_status(sync_engine, case.submission_id) == "ingest_failed"
    job = jobs_of_kind(sync_engine, "ingest_ugc_file")[0]
    assert job.last_error == "terminal_business_error:file_not_found"
    notify = jobs_of_kind(sync_engine, "notify_ingest_failed")
    assert len(notify) == 1
    assert notify[0].payload["system_reason_slug"] == "file_not_found"
    assert notify[0].payload["attempt_closed"] is False
    assert jobs_of_kind(sync_engine, "notify_ugc_rejected") == []


async def test_telegram_too_large_is_terminal_file_too_large(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Ревью G: 400 "file is too big" от Telegram - file_too_large, а не file_not_found."""
    case = await brand_b_submission(uow_factory, sync_engine, 7010)
    files = FakeFiles(exc=TelegramFileTooLarge())

    await _run_ingest(uow_factory, _deps(files, FakeStorage(), FakeSniffer("image/jpeg")))

    assert submission_status(sync_engine, case.submission_id) == "ingest_failed"
    job = jobs_of_kind(sync_engine, "ingest_ugc_file")[0]
    assert job.last_error == "terminal_business_error:file_too_large"
    notify = jobs_of_kind(sync_engine, "notify_ingest_failed")
    assert notify[0].payload["system_reason_slug"] == "file_too_large"


async def test_ingest_transient_telegram_error_is_retried(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """429/5xx Telegram: задача возвращается в queued, подача остаётся receiving."""
    case = await brand_b_submission(uow_factory, sync_engine, 7005)
    files = FakeFiles(exc=TelegramFileTransient())

    await _run_ingest(uow_factory, _deps(files, FakeStorage(), FakeSniffer("image/jpeg")))

    assert submission_status(sync_engine, case.submission_id) == "receiving"
    job = jobs_of_kind(sync_engine, "ingest_ugc_file")[0]
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.last_error is None or job.last_error == "telegram_unavailable"


async def test_ingest_storage_unavailable_is_retried(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    case = await brand_b_submission(uow_factory, sync_engine, 7006)
    storage = FakeStorage(exc=ObjectStorageUnavailable())

    await _run_ingest(uow_factory, _deps(FakeFiles(), storage, FakeSniffer("image/jpeg")))

    assert submission_status(sync_engine, case.submission_id) == "receiving"
    assert jobs_of_kind(sync_engine, "ingest_ugc_file")[0].status == "queued"


async def test_exhausted_ingest_goes_dead_and_marks_failed(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Исчерпание ретраев: dead-хук в той же транзакции - ingest_failed и системное уведомление."""
    case = await brand_b_submission(uow_factory, sync_engine, 7007)
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET attempts = 5 WHERE kind = 'ingest_ugc_file'"))
    files = FakeFiles(exc=TelegramFileTransient())

    await _run_ingest(uow_factory, _deps(files, FakeStorage(), FakeSniffer("image/jpeg")))

    job = jobs_of_kind(sync_engine, "ingest_ugc_file")[0]
    assert job.status == "dead"
    assert submission_status(sync_engine, case.submission_id) == "ingest_failed"
    notify = jobs_of_kind(sync_engine, "notify_ingest_failed")
    assert notify[0].payload["system_reason_slug"] == "ingest_dead"


async def test_ingest_oversize_file_is_terminal(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """Фактический размер сверяется с max_file_bytes из лимитов активности (5 МБ в сиде)."""
    case = await brand_b_submission(uow_factory, sync_engine, 7008)
    big = b"\xff\xd8" + b"0" * (5 * 1024 * 1024 + 1)
    files = FakeFiles(result=(big, "image/jpeg"))

    await _run_ingest(uow_factory, _deps(files, FakeStorage(), FakeSniffer("image/jpeg")))

    assert submission_status(sync_engine, case.submission_id) == "ingest_failed"
    job = jobs_of_kind(sync_engine, "ingest_ugc_file")[0]
    assert job.last_error == "terminal_business_error:file_too_large"
