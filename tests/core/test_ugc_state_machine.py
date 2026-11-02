"""Юнит-часть фазы 3a: T7 и сбой загрузки (F4), страховка индексом (F1), local_fs (T8).

Переходы идут через настоящее ядро и Postgres; для local_fs - настоящая папка tmp_path.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.adapters.object_storage.local_fs import LocalFsObjectStorage
from app.common.errors import ConflictError, ValidationError
from app.core.activities.ugc import accept_from_user, ingest_file_failed, resubmit, start_ugc
from app.core.activities.ugc.state_machine import can_transition
from app.core.types.ids import AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from tests.factories import create_attempt, create_guest
from tests.factories.survey import UowFactory
from tests.factories.ugc import attempt_status, count_rows, job_rows, ugc_rows

SLUG = "brand_b_photo_subscription"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
TG_FILE_ID = "AgACAgIAAxkBAAIBSTATE"
POST_URL = "https://www.instagram.com/p/example/"
PROFILE_URL = "https://www.instagram.com/example/"
TEST_SIGNING_KEY = "ugc-test-signing-material-xxxxxxxxxxxxxxxxxxxx"
MEDIA_URL_RE = re.compile(r"/media/[A-Za-z0-9_-]+\?t=[A-Za-z0-9_.\-]+")


async def _start(uow_factory: UowFactory, user_id: int, channel_id: int) -> int:
    async with uow_factory() as uow:
        r = uow.repositories
        started = await start_ugc(
            r.consents,
            r.catalog,
            r.attempts,
            UserId(user_id),
            SLUG,
            ChannelAccountId(channel_id),
            NOW,
        )
    return int(started.attempt_id)


async def _accept(uow_factory: UowFactory, user_id: int, attempt_id: int) -> int:
    async with uow_factory() as uow:
        r = uow.repositories
        accepted = await accept_from_user(
            r.consents,
            r.catalog,
            r.attempts,
            r.ugc,
            r.jobs,
            UserId(user_id),
            AttemptId(attempt_id),
            TG_FILE_ID,
            "image/jpeg",
            102400,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    return int(accepted.submission_id)


async def _ingest_failed(
    uow_factory: UowFactory, submission_id: int, reason: str = "download_error"
) -> None:
    async with uow_factory() as uow:
        r = uow.repositories
        await ingest_file_failed(
            r.ugc, r.attempts, r.catalog, r.jobs, UgcSubmissionId(submission_id), reason, NOW
        )


async def _resubmit(uow_factory: UowFactory, user_id: int, attempt_id: int, tg_file_id: str) -> int:
    async with uow_factory() as uow:
        r = uow.repositories
        result = await resubmit(
            r.consents,
            r.catalog,
            r.attempts,
            r.ugc,
            r.jobs,
            UserId(user_id),
            AttemptId(attempt_id),
            tg_file_id,
            "image/jpeg",
            102400,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    assert result.kind == "success", result
    assert result.submission_id is not None
    return int(result.submission_id)


# ---------------------------------------------------------------- T7 (F4)


async def test_ingest_file_failed_keeps_attempt_pending_and_enqueues_notify(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """T7 (F4, пересмотр N3): ingest_failed у строки; прохождение остаётся pending_moderation;
    задача уведомления несёт user_id и reason_slug.
    """
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    submission_id = await _accept(uow_factory, guest.user_id, attempt_id)

    await _ingest_failed(uow_factory, submission_id)

    assert ugc_rows(sync_engine, attempt_id)[0].status == "ingest_failed"
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"
    notify = job_rows(sync_engine, "notify_ingest_failed")
    assert len(notify) == 1
    assert notify[0].payload["system_reason_slug"] == "download_error"
    assert notify[0].payload["user_id"] == guest.user_id


async def test_ingest_failed_then_resubmit_succeeds(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """F4: гость получает уведомление и делает resubmit: новая receiving и задача ingest."""
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    failed = await _accept(uow_factory, guest.user_id, attempt_id)
    await _ingest_failed(uow_factory, failed)

    retry = await _resubmit(uow_factory, guest.user_id, attempt_id, "retry_file")

    rows = ugc_rows(sync_engine, attempt_id)
    assert [(row.id, row.status) for row in rows] == [
        (failed, "ingest_failed"),
        (retry, "receiving"),
    ]
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"


async def test_ingest_failed_keeps_day_busy_until_resubmit(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """F4: прохождение на модерации держит день (attempts_once_per_day): новый старт отклоняется."""
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    submission_id = await _accept(uow_factory, guest.user_id, attempt_id)
    await _ingest_failed(uow_factory, submission_id)

    with pytest.raises(ConflictError) as exc:
        await _start(uow_factory, guest.user_id, guest.channel_account_id)

    assert exc.value.code == "already_passed_today"
    assert count_rows(sync_engine, "attempts") == 1


async def test_ingest_file_failed_rejects_free_text_reason(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """F3: причина сбоя - слаг воркера. Свободный текст отклоняется до записи."""
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    submission_id = await _accept(uow_factory, guest.user_id, attempt_id)

    with pytest.raises(ValidationError) as exc:
        await _ingest_failed(uow_factory, submission_id, "Файл не загрузился, попробуйте ещё раз")

    assert exc.value.code == "reason_invalid_slug"
    assert ugc_rows(sync_engine, attempt_id)[0].status == "receiving"
    assert job_rows(sync_engine, "notify_ugc_rejected") == []


async def test_second_accept_on_same_attempt_conflicts(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """Пока фото на модерации, прохождение не в in_progress: второй accept - 409."""
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    await _accept(uow_factory, guest.user_id, attempt_id)

    with pytest.raises(ConflictError) as exc:
        await _accept(uow_factory, guest.user_id, attempt_id)

    assert exc.value.code == "attempt_not_in_progress"
    assert count_rows(sync_engine, "ugc_submissions") == 1


# ---------------------------------------------------------------- F1: страховка индексом


async def test_db_index_forbids_second_active_submission(
    sync_engine: Engine, clean_db: None
) -> None:
    """F1: даже без блокировки ядра БД не пропустит вторую активную подачу на прохождение."""
    guest = create_guest(sync_engine)
    attempt_id = create_attempt(sync_engine, guest, SLUG, status="pending_moderation")
    insert = text(
        "INSERT INTO ugc_submissions (attempt_id, status, submitted_at, created_at, updated_at) "
        "VALUES (:aid, 'receiving', now(), now(), now())"
    )
    with sync_engine.begin() as conn:
        conn.execute(insert, {"aid": attempt_id})

    with pytest.raises(IntegrityError) as exc:
        with sync_engine.begin() as conn:
            conn.execute(insert, {"aid": attempt_id})

    assert "ugc_submission_active" in str(exc.value)


async def test_repository_turns_active_index_conflict_into_submission_active(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    """F1: без блокировки гонку ловит индекс; репозиторий отдаёт ConflictError submission_active."""
    guest = create_guest(sync_engine)
    attempt_id = create_attempt(sync_engine, guest, SLUG, status="pending_moderation")
    async with uow_factory() as uow:
        await uow.repositories.ugc.insert_receiving(AttemptId(attempt_id), None, None, NOW)

    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await uow.repositories.ugc.insert_receiving(AttemptId(attempt_id), None, None, NOW)

    assert exc.value.code == "submission_active"
    assert count_rows(sync_engine, "ugc_submissions") == 1


@pytest.mark.parametrize(
    ("src", "dst", "allowed"),
    [
        ("receiving", "pending", True),
        ("receiving", "ingest_failed", True),
        ("pending", "approved", True),
        ("pending", "rejected", True),
        # Повтор после отказа - новая строка, а не переход строки (04 §5.2, 02 §13).
        ("rejected", "pending", False),
        ("approved", "rejected", False),
        ("ingest_failed", "pending", False),
        ("pending", "receiving", False),
    ],
)
def test_ugc_transitions_table(src: str, dst: str, allowed: bool) -> None:
    assert can_transition(src, dst) is allowed  # type: ignore[arg-type]


# ---------------------------------------------------------------- T8


async def test_object_storage_local_fs_round_trip(tmp_path: Path) -> None:
    storage = LocalFsObjectStorage(root=tmp_path, signing_key=TEST_SIGNING_KEY)

    await storage.put("ugc", "key", b"hello", "image/jpeg")

    assert await storage.get("ugc", "key") == b"hello"
    url = storage.presigned_get("ugc", "key", 300)
    assert MEDIA_URL_RE.fullmatch(url)
    assert storage.verify_presigned(url) == ("ugc", "key")


async def test_local_fs_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalFsObjectStorage(root=tmp_path / "store", signing_key=TEST_SIGNING_KEY)

    with pytest.raises(ValidationError) as key_exc:
        await storage.put("ugc", "../escape.jpg", b"x", "image/jpeg")
    assert key_exc.value.code == "object_key_invalid"

    with pytest.raises(ValidationError) as bucket_exc:
        await storage.put("../ugc", "a.jpg", b"x", "image/jpeg")
    assert bucket_exc.value.code == "object_bucket_invalid"
    assert not (tmp_path / "escape.jpg").exists()


def test_local_fs_presigned_link_expires_and_rejects_tampering(tmp_path: Path) -> None:
    clock = {"now": 1_800_000_000.0}
    storage = LocalFsObjectStorage(
        root=tmp_path, signing_key=TEST_SIGNING_KEY, now_fn=lambda: clock["now"]
    )
    url = storage.presigned_get("ugc", "key.jpg", 60)
    other = storage.presigned_get("ugc", "other.jpg", 60)
    signed = url.split("?t=", 1)[1]
    other_id = other.split("/media/", 1)[1].split("?", 1)[0]

    # Подмена id при чужой подписи: подпись не сходится.
    with pytest.raises(ValidationError) as tamper_exc:
        storage.verify_presigned(f"/media/{other_id}?t={signed}")
    assert tamper_exc.value.code == "media_link_invalid"

    clock["now"] += 61
    with pytest.raises(ValidationError) as expired_exc:
        storage.verify_presigned(url)
    assert expired_exc.value.code == "media_link_expired"
