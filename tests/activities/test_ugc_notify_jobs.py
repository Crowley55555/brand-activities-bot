"""Ядро фазы 3b: задачи уведомлений (ADR-25), причина отказа из данных (Q4), приёмка по декларации.

notify_reward: payload - reward_id ваучера (Q2). notify_ugc_rejected: attempt_closed считается по
лимиту повторов (Q3) и для отказа модератора, и для сбоя загрузки (ingest_failed). Сумма строк
прохождения не превышает max_resubmits + 1, поэтому повтора после attempt_closed=true не будет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine

from app.common.errors import ValidationError
from app.core.activities.ugc import (
    accept_from_user,
    approve,
    ingest_file_done,
    ingest_file_failed,
    reject,
    resubmit,
    start_ugc,
)
from app.core.types.ids import AdminId, AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from tests.factories import Guest, create_guest
from tests.factories.survey import UowFactory
from tests.factories.ugc import attempt_status, create_admin, job_rows, reward_rows, ugc_rows

SLUG = "brand_b_photo_subscription"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
TG_FILE_ID = "AgACAgIAAxkBAAIBNOTIFY"
POST_URL = "https://www.instagram.com/p/example/"
PROFILE_URL = "https://www.instagram.com/example/"
MIME_JPEG = "image/jpeg"
SIZE_OK = 102400


async def _start(uow_factory: UowFactory, guest: Guest) -> int:
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
    return int(started.attempt_id)


async def _accept(
    uow_factory: UowFactory,
    guest: Guest,
    attempt_id: int,
    *,
    mime: str = MIME_JPEG,
    size: int = SIZE_OK,
) -> int:
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
            mime,
            size,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    return int(accepted.submission_id)


async def _resubmit(uow_factory: UowFactory, guest: Guest, attempt_id: int) -> int:
    async with uow_factory() as uow:
        r = uow.repositories
        result = await resubmit(
            r.consents,
            r.catalog,
            r.attempts,
            r.ugc,
            r.jobs,
            UserId(guest.user_id),
            AttemptId(attempt_id),
            TG_FILE_ID,
            MIME_JPEG,
            SIZE_OK,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    assert result.kind == "success", result
    assert result.submission_id is not None
    return int(result.submission_id)


async def _ingest_done(uow_factory: UowFactory, submission_id: int) -> None:
    async with uow_factory() as uow:
        await ingest_file_done(
            uow.repositories.ugc,
            UgcSubmissionId(submission_id),
            "ugc",
            f"{uuid.uuid4()}.jpg",
            MIME_JPEG,
            SIZE_OK,
        )


async def _pending(
    uow_factory: UowFactory, engine: Engine, external_id: str
) -> tuple[Guest, int, int]:
    guest = create_guest(engine, external_id)
    attempt_id = await _start(uow_factory, guest)
    submission_id = await _accept(uow_factory, guest, attempt_id)
    await _ingest_done(uow_factory, submission_id)
    return guest, attempt_id, submission_id


async def _reject(uow_factory: UowFactory, admin_id: int, submission_id: int, slug: str) -> None:
    async with uow_factory() as uow:
        r = uow.repositories
        await reject(
            r.ugc,
            r.attempts,
            r.catalog,
            r.jobs,
            UgcSubmissionId(submission_id),
            AdminId(admin_id),
            slug,
            None,
            NOW,
        )


# ---------------------------------------------------------------- notify_reward (ADR-25, Q2)


async def test_approve_enqueues_notify_reward_with_voucher_id(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    _, attempt_id, submission_id = await _pending(uow_factory, sync_engine, "1101")
    admin_id = create_admin(sync_engine)

    async with uow_factory() as uow:
        r = uow.repositories
        await approve(
            r.ugc,
            r.attempts,
            r.catalog,
            r.rewards,
            r.jobs,
            UgcSubmissionId(submission_id),
            AdminId(admin_id),
            NOW,
        )

    voucher = next(
        row for row in reward_rows(sync_engine, attempt_id) if row.reward_type == "voucher"
    )
    notify = job_rows(sync_engine, "notify_reward")
    assert len(notify) == 1
    assert notify[0].payload == {"reward_id": int(voucher.id)}


# ---------------------------------------------------------------- notify_ugc_rejected (Q3, Q4)


async def test_reject_first_refusal_has_attempt_closed_false(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """Первый отказ: строк 1 < max_resubmits + 1 = 2, повтор ещё возможен."""
    guest, attempt_id, submission_id = await _pending(uow_factory, sync_engine, "1201")
    admin_id = create_admin(sync_engine)

    await _reject(uow_factory, admin_id, submission_id, "low_quality")

    (notify,) = job_rows(sync_engine, "notify_ugc_rejected")
    assert notify.payload == {
        "user_id": guest.user_id,
        "reason_slug": "low_quality",
        "attempt_closed": False,
        "attempt_id": attempt_id,
    }


async def test_reject_last_allowed_refusal_has_attempt_closed_true(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """Второй отказ при max_resubmits=1: строк 2 >= 2 - повтора не будет, attempt_closed=true."""
    guest, attempt_id, first = await _pending(uow_factory, sync_engine, "1202")
    admin_id = create_admin(sync_engine)
    await _reject(uow_factory, admin_id, first, "off_topic")
    second = await _resubmit(uow_factory, guest, attempt_id)
    await _ingest_done(uow_factory, second)

    await _reject(uow_factory, admin_id, second, "no_post_link")

    notify = job_rows(sync_engine, "notify_ugc_rejected")
    assert [row.payload["attempt_closed"] for row in notify] == [False, True]
    assert notify[1].payload["reason_slug"] == "no_post_link"


async def test_reject_unknown_reason_slug_is_validation_error(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """Q4: причина берётся из activity.limits.reject_reasons; чужой слаг - 422-ошибка ядра."""
    _, attempt_id, submission_id = await _pending(uow_factory, sync_engine, "1203")
    admin_id = create_admin(sync_engine)

    with pytest.raises(ValidationError) as exc_info:
        await _reject(uow_factory, admin_id, submission_id, "made_up_reason")

    assert exc_info.value.code == "reason_slug_unknown"
    (row,) = ugc_rows(sync_engine, attempt_id)
    assert row.status == "pending"
    assert job_rows(sync_engine, "notify_ugc_rejected") == []


async def test_ingest_failed_payload_carries_attempt_closed(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """Сбой загрузки тоже считается в лимит: первый - false, второй (лимит исчерпан) - true."""
    guest = create_guest(sync_engine, "1204")
    attempt_id = await _start(uow_factory, guest)
    first = await _accept(uow_factory, guest, attempt_id)
    async with uow_factory() as uow:
        r = uow.repositories
        await ingest_file_failed(
            r.ugc, r.attempts, r.catalog, r.jobs, UgcSubmissionId(first), "download_error", NOW
        )
    second = await _resubmit(uow_factory, guest, attempt_id)
    async with uow_factory() as uow:
        r = uow.repositories
        await ingest_file_failed(
            r.ugc, r.attempts, r.catalog, r.jobs, UgcSubmissionId(second), "download_error", NOW
        )

    notify = job_rows(sync_engine, "notify_ingest_failed")
    assert [row.payload["attempt_closed"] for row in notify] == [False, True]


# ---------------------------------------------------------------- приёмка по декларации (Q1)


async def test_accept_rejects_declared_mime_before_any_write(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, "1301")
    attempt_id = await _start(uow_factory, guest)

    with pytest.raises(ValidationError) as exc_info:
        await _accept(uow_factory, guest, attempt_id, mime="application/pdf")

    assert exc_info.value.code == "mime_not_allowed"
    assert ugc_rows(sync_engine, attempt_id) == []
    assert job_rows(sync_engine, "ingest_ugc_file") == []


async def test_accept_rejects_declared_size_over_limit(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine, "1302")
    attempt_id = await _start(uow_factory, guest)

    with pytest.raises(ValidationError) as exc_info:
        await _accept(uow_factory, guest, attempt_id, size=10485760)

    assert exc_info.value.code == "file_too_large"
    assert ugc_rows(sync_engine, attempt_id) == []


async def test_resubmit_checks_declared_mime_too(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    guest, attempt_id, first = await _pending(uow_factory, sync_engine, "1303")
    admin_id = create_admin(sync_engine)
    await _reject(uow_factory, admin_id, first, "low_quality")

    with pytest.raises(ValidationError) as exc_info:
        async with uow_factory() as uow:
            r = uow.repositories
            await resubmit(
                r.consents,
                r.catalog,
                r.attempts,
                r.ugc,
                r.jobs,
                UserId(guest.user_id),
                AttemptId(attempt_id),
                TG_FILE_ID,
                "application/pdf",
                SIZE_OK,
                POST_URL,
                PROFILE_URL,
                NOW,
            )

    assert exc_info.value.code == "mime_not_allowed"
    assert len(ugc_rows(sync_engine, attempt_id)) == 1


async def test_last_reject_leaves_attempt_pending_moderation(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """Замечание 4 (вариант б, lazy close): после финального отказа прохождение не закрывается.

    attempt_closed=true уходит в уведомление. Прохождение закроется rejected при resubmit
    (он даёт limit_exceeded) или джобом expire_stale_pending_moderation (фаза 7). Контракт N4
    не меняется, поэтому здесь явно проверяем статус, а не limit_exceeded.
    """
    guest, attempt_id, first = await _pending(uow_factory, sync_engine, "1205")
    admin_id = create_admin(sync_engine)
    await _reject(uow_factory, admin_id, first, "off_topic")
    second = await _resubmit(uow_factory, guest, attempt_id)
    await _ingest_done(uow_factory, second)

    await _reject(uow_factory, admin_id, second, "no_post_link")

    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"
    notify = job_rows(sync_engine, "notify_ugc_rejected")
    assert notify[-1].payload["attempt_closed"] is True
