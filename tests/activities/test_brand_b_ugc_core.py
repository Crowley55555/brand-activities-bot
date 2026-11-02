"""Приёмочные тесты фазы 3a: Бренд B, фото + ссылка на пост + ссылка на профиль.

T1-T5 - приёмка и решение модератора. T6 - отказ, повтор новым фото (resubmit) и одобрение
второй строки. T6b - лимит повторов исчерпан (N4: результат, не исключение). T6c - после
финального отказа день занят (N5, ADR-19). Идём через настоящее ядро и настоящий Postgres
(как test_brand_a_survey.py), без моков. Slug активности и её limits - из сид-миграции 0007
(правило 5); здесь это только тестовые данные.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError
from app.core.activities.ugc import (
    accept_from_user,
    approve,
    ingest_file_done,
    ingest_file_failed,
    reject,
    resubmit,
    start_ugc,
)
from app.core.consent import service as consent_service
from app.core.types.ids import AdminId, AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from app.core.types.results import ResubmitResult
from tests.factories import create_guest
from tests.factories.survey import UowFactory
from tests.factories.ugc import (
    attempt_status,
    count_rows,
    create_admin,
    job_rows,
    reward_rows,
    ugc_rows,
)

SLUG = "brand_b_photo_subscription"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
TG_FILE_ID = "AgACAgIAAxkBAAIBTESTFILE"
NEW_TG_FILE_ID = "AgACAgIAAxkBAAIBNEWFILE"
MIME_JPEG = "image/jpeg"
SIZE_OK = 102400
POST_URL = "https://www.instagram.com/p/example/"
PROFILE_URL = "https://www.instagram.com/example/"
VOUCHER_RE = re.compile(r"^VOUCHER-[A-Z0-9]{8}$")
PROMO_RE = re.compile(r"^BRAND_B-[A-Z0-9]{8}$")


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
            MIME_JPEG,
            SIZE_OK,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    return int(accepted.submission_id)


async def _resubmit_result(
    uow_factory: UowFactory, user_id: int, attempt_id: int, tg_file_id: str
) -> ResubmitResult:
    async with uow_factory() as uow:
        r = uow.repositories
        return await resubmit(
            r.consents,
            r.catalog,
            r.attempts,
            r.ugc,
            r.jobs,
            UserId(user_id),
            AttemptId(attempt_id),
            tg_file_id,
            MIME_JPEG,
            SIZE_OK,
            POST_URL,
            PROFILE_URL,
            NOW,
        )


async def _resubmit(
    uow_factory: UowFactory, user_id: int, attempt_id: int, tg_file_id: str = NEW_TG_FILE_ID
) -> int:
    result = await _resubmit_result(uow_factory, user_id, attempt_id, tg_file_id)
    assert result.kind == "success", result
    assert result.submission_id is not None
    return int(result.submission_id)


async def _ingest_done(uow_factory: UowFactory, submission_id: int) -> str:
    """Имитирует воркер ingest (фаза 3c): файл лёг в хранилище. Возвращает ключ."""
    key = f"{uuid.uuid4()}.jpg"
    async with uow_factory() as uow:
        await ingest_file_done(
            uow.repositories.ugc,
            UgcSubmissionId(submission_id),
            "ugc",
            key,
            "image/jpeg",
            102400,
        )
    return key


async def _reject(uow_factory: UowFactory, submission_id: int, admin_id: int) -> None:
    async with uow_factory() as uow:
        r = uow.repositories
        await reject(
            r.ugc,
            r.attempts,
            r.catalog,
            r.jobs,
            UgcSubmissionId(submission_id),
            AdminId(admin_id),
            "low_quality",
            None,
            NOW,
        )


async def _approve(uow_factory: UowFactory, submission_id: int, admin_id: int) -> None:
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


async def _pending_submission(uow_factory: UowFactory, sync_engine: Engine) -> tuple[int, int, int]:
    """Гость с согласием -> прохождение -> принятое фото -> pending. (user, attempt, sub)."""
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    submission_id = await _accept(uow_factory, guest.user_id, attempt_id)
    await _ingest_done(uow_factory, submission_id)
    return guest.user_id, attempt_id, submission_id


# ---------------------------------------------------------------- T1


async def test_accept_without_consent_rejects_409(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """Правило 1: accept_from_user первой строкой требует согласие.

    Порядок (пересмотр оркестратора): согласие -> start (успех) -> отзыв согласия -> accept.
    Сам start без согласия не пройдёт (start_attempt зовёт require_consent), поэтому
    сначала согласие есть, а отзыв делаем перед accept.
    """
    guest = create_guest(sync_engine, with_consent=False)
    async with uow_factory() as uow:
        await consent_service.accept(
            uow.repositories.consents, UserId(guest.user_id), "telegram", NOW
        )
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    async with uow_factory() as uow:
        await consent_service.revoke(uow.repositories.consents, UserId(guest.user_id), NOW)

    with pytest.raises(ConflictError) as exc:
        await _accept(uow_factory, guest.user_id, attempt_id)

    assert exc.value.code == "consent_required"
    assert count_rows(sync_engine, "ugc_submissions") == 0
    assert count_rows(sync_engine, "jobs") == 0
    assert attempt_status(sync_engine, attempt_id) == "in_progress"


# ---------------------------------------------------------------- T2


async def test_accept_from_user_creates_receiving_submission_and_job(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)

    submission_id = await _accept(uow_factory, guest.user_id, attempt_id)

    rows = ugc_rows(sync_engine, attempt_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == submission_id
    assert row.status == "receiving"
    assert row.post_url == POST_URL
    assert row.subscription_claimed_url == PROFILE_URL
    assert row.submitted_at is not None
    assert row.decided_at is None
    # Файл появится только после ingest (T3): до этого все file_* пустые.
    assert (row.file_bucket, row.file_key, row.file_content_type, row.file_size_bytes) == (
        None,
        None,
        None,
        None,
    )

    jobs = job_rows(sync_engine, "ingest_ugc_file")
    assert len(jobs) == 1
    assert jobs[0].idempotency_key == f"ugc_ingest:{submission_id}"
    # tg_file_id живёт в payload задачи, а не в ugc_submissions (канон 08 §4.1, 04 §4.6).
    assert jobs[0].payload["tg_file_id"] == TG_FILE_ID
    assert jobs[0].payload["submission_id"] == submission_id
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"


# ---------------------------------------------------------------- T3


async def test_ingest_file_done_transitions_to_pending(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    submission_id = await _accept(uow_factory, guest.user_id, attempt_id)

    key = await _ingest_done(uow_factory, submission_id)

    row = ugc_rows(sync_engine, attempt_id)[0]
    assert row.status == "pending"
    assert row.file_bucket == "ugc"
    assert row.file_key == key
    assert row.file_content_type == "image/jpeg"
    assert row.file_size_bytes == 102400
    assert row.decided_at is None


# ---------------------------------------------------------------- T4


async def test_approve_grants_voucher_and_promocode(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    user_id, attempt_id, submission_id = await _pending_submission(uow_factory, sync_engine)
    admin_id = create_admin(sync_engine)

    await _approve(uow_factory, submission_id, admin_id)

    row = ugc_rows(sync_engine, attempt_id)[0]
    assert row.status == "approved"
    assert row.decided_by_admin_id == admin_id
    assert row.decided_at is not None

    by_type = {reward.reward_type: reward for reward in reward_rows(sync_engine, attempt_id)}
    assert set(by_type) == {"voucher", "promocode"}
    assert by_type["voucher"].title == "Напиток дня"
    # По решению оркестратора у ваучера есть код (CHECK rewards_code_matches_type).
    assert VOUCHER_RE.match(by_type["voucher"].code)
    assert PROMO_RE.match(by_type["promocode"].code)
    assert attempt_status(sync_engine, attempt_id) == "completed"
    assert user_id > 0


# ---------------------------------------------------------------- T5


async def test_duplicate_approve_rejects(uow_factory: UowFactory, sync_engine: Engine) -> None:
    _, attempt_id, submission_id = await _pending_submission(uow_factory, sync_engine)
    admin_id = create_admin(sync_engine)
    await _approve(uow_factory, submission_id, admin_id)

    with pytest.raises(ConflictError) as exc:
        await _approve(uow_factory, submission_id, admin_id)

    assert exc.value.code == "not_pending"
    assert len(reward_rows(sync_engine, attempt_id)) == 2


# ---------------------------------------------------------------- reject (N1)


async def test_reject_marks_submission_and_keeps_attempt_pending(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """N1 (решение оркестратора): отказ не закрывает прохождение - гость может повторить."""
    _, attempt_id, submission_id = await _pending_submission(uow_factory, sync_engine)
    admin_id = create_admin(sync_engine)

    await _reject(uow_factory, submission_id, admin_id)

    row = ugc_rows(sync_engine, attempt_id)[0]
    assert row.status == "rejected"
    assert row.rejection_reason_slug == "low_quality"
    assert row.decided_by_admin_id == admin_id
    assert row.decided_at is not None
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"
    assert reward_rows(sync_engine, attempt_id) == []


# ---------------------------------------------------------------- T6 (N2)


async def test_reject_then_resubmit_then_approve(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """T6: отказ -> повтор новым фото (receiving + ingest) -> одобрение второй строки."""
    user_id, attempt_id, first = await _pending_submission(uow_factory, sync_engine)
    admin_id = create_admin(sync_engine)
    await _reject(uow_factory, first, admin_id)
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"

    second = await _resubmit(uow_factory, user_id, attempt_id, NEW_TG_FILE_ID)

    rows = ugc_rows(sync_engine, attempt_id)
    assert [(row.id, row.status) for row in rows] == [(first, "rejected"), (second, "receiving")]
    ingest_jobs = job_rows(sync_engine, "ingest_ugc_file")
    assert [job.payload["tg_file_id"] for job in ingest_jobs] == [TG_FILE_ID, NEW_TG_FILE_ID]
    assert ingest_jobs[-1].payload["submission_id"] == second
    assert ingest_jobs[-1].idempotency_key == f"ugc_ingest:{second}"

    await _ingest_done(uow_factory, second)
    assert ugc_rows(sync_engine, attempt_id)[-1].status == "pending"

    await _approve(uow_factory, second, admin_id)

    last = ugc_rows(sync_engine, attempt_id)[-1]
    assert last.status == "approved"
    assert last.decided_by_admin_id == admin_id
    rewards = reward_rows(sync_engine, attempt_id)
    assert {reward.reward_type for reward in rewards} == {"voucher", "promocode"}
    assert len(rewards) == 2
    assert attempt_status(sync_engine, attempt_id) == "completed"


# ---------------------------------------------------------------- T6b (N4)


async def test_resubmit_limit_closes_attempt_as_rejected(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """T6b: max_resubmits = 1 -> две подачи; третья - limit_exceeded, прохождение rejected.

    Лимит - результат use-case (N4). Исключения нет, поэтому закрытие прохождения фиксируется:
    UoW коммитит при нормальном выходе. Строк на прохождении ровно max_resubmits + 1 = 2.
    """
    user_id, attempt_id, first = await _pending_submission(uow_factory, sync_engine)
    admin_id = create_admin(sync_engine)
    await _reject(uow_factory, first, admin_id)
    second = await _resubmit(uow_factory, user_id, attempt_id, "second_file")
    await _ingest_done(uow_factory, second)
    await _reject(uow_factory, second, admin_id)

    result = await _resubmit_result(uow_factory, user_id, attempt_id, "third_file")

    assert result.kind == "limit_exceeded"
    assert result.attempt_closed is True
    assert result.submission_id is None
    assert attempt_status(sync_engine, attempt_id) == "rejected"
    rows = ugc_rows(sync_engine, attempt_id)
    assert len(rows) == 2
    assert [row.status for row in rows] == ["rejected", "rejected"]
    assert len(job_rows(sync_engine, "ingest_ugc_file")) == 2


# ---------------------------------------------------------------- T6c (N5, ADR-19)


async def test_rejected_blocks_new_attempt_same_day(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """T6c: после финального отказа (limit_exceeded, attempt rejected) новый старт того же дня
    отклоняется. Без `rejected` в attempts_once_per_day гость обходил бы лимит повторов.
    """
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    first = await _accept(uow_factory, guest.user_id, attempt_id)
    await _ingest_done(uow_factory, first)
    admin_id = create_admin(sync_engine)
    await _reject(uow_factory, first, admin_id)
    second = await _resubmit(uow_factory, guest.user_id, attempt_id, NEW_TG_FILE_ID)
    await _ingest_done(uow_factory, second)
    await _reject(uow_factory, second, admin_id)
    result = await _resubmit_result(uow_factory, guest.user_id, attempt_id, "third_file")
    assert result.kind == "limit_exceeded"
    assert attempt_status(sync_engine, attempt_id) == "rejected"

    with pytest.raises(ConflictError) as exc:
        await _start(uow_factory, guest.user_id, guest.channel_account_id)

    assert exc.value.code == "already_passed_today"
    assert count_rows(sync_engine, "attempts") == 1


async def test_resubmit_while_submission_is_active_conflicts(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """Допущение: повтор возможен только после отказа; пока фото на модерации - 409."""
    user_id, attempt_id, _ = await _pending_submission(uow_factory, sync_engine)

    with pytest.raises(ConflictError) as exc:
        await _resubmit(uow_factory, user_id, attempt_id, "new_file")

    assert exc.value.code == "submission_active"
    assert count_rows(sync_engine, "ugc_submissions") == 1
    assert len(job_rows(sync_engine, "ingest_ugc_file")) == 1


# ---------------------------------------------------------------- F1, F4, F7 (ревью фазы 3a)


async def _ingest_failed(uow_factory: UowFactory, submission_id: int) -> None:
    async with uow_factory() as uow:
        r = uow.repositories
        await ingest_file_failed(
            r.ugc,
            r.attempts,
            r.catalog,
            r.jobs,
            UgcSubmissionId(submission_id),
            "download_error",
            NOW,
        )


async def test_concurrent_resubmit_rejects_second_with_submission_active(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """F1: параллельные повторы одного прохождения - один success, второй submission_active.

    По ревью для проигравшего допустим и limit_exceeded; здесь лимит не исчерпан.
    """
    user_id, attempt_id, first = await _pending_submission(uow_factory, sync_engine)
    admin_id = create_admin(sync_engine)
    await _reject(uow_factory, first, admin_id)

    async def try_resubmit(tg_file_id: str) -> ResubmitResult | ConflictError:
        try:
            return await _resubmit_result(uow_factory, user_id, attempt_id, tg_file_id)
        except ConflictError as exc:
            return exc

    outcomes = await asyncio.gather(try_resubmit("file_a"), try_resubmit("file_b"))

    wins = [o for o in outcomes if isinstance(o, ResubmitResult) and o.kind == "success"]
    losses = [o for o in outcomes if o not in wins]
    assert len(wins) == 1
    assert len(losses) == 1
    loser = losses[0]
    if isinstance(loser, ConflictError):
        assert loser.code == "submission_active"
    else:
        assert loser.kind == "limit_exceeded"
    assert [row.status for row in ugc_rows(sync_engine, attempt_id)] == ["rejected", "receiving"]
    assert len(job_rows(sync_engine, "ingest_ugc_file")) == 2


async def test_two_ingest_failures_exhaust_resubmit_limit(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """F4 (T6d): ingest_failed считается в max_resubmits. После двух сбоев повтор - limit_exceeded.

    Отказов модератора нет: лимит исчерпан только сбоями загрузки.
    """
    guest = create_guest(sync_engine, external_id="1003")
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    first = await _accept(uow_factory, guest.user_id, attempt_id)
    await _ingest_failed(uow_factory, first)
    await _resubmit(uow_factory, guest.user_id, attempt_id, "second_file")
    second = ugc_rows(sync_engine, attempt_id)[-1].id
    await _ingest_failed(uow_factory, second)

    result = await _resubmit_result(uow_factory, guest.user_id, attempt_id, "third_file")

    assert result.kind == "limit_exceeded"
    assert attempt_status(sync_engine, attempt_id) == "rejected"
    assert [row.status for row in ugc_rows(sync_engine, attempt_id)] == [
        "ingest_failed",
        "ingest_failed",
    ]


async def test_reject_rejects_non_pending_submission(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """F7: решение только для pending. Отказ по ingest_failed и по approved - 409 not_pending."""
    guest = create_guest(sync_engine, external_id="1004")
    attempt_id = await _start(uow_factory, guest.user_id, guest.channel_account_id)
    failed = await _accept(uow_factory, guest.user_id, attempt_id)
    await _ingest_failed(uow_factory, failed)
    admin_id = create_admin(sync_engine)

    with pytest.raises(ConflictError) as exc:
        await _reject(uow_factory, failed, admin_id)
    assert exc.value.code == "not_pending"

    other = create_guest(sync_engine, external_id="1005")
    other_attempt = await _start(uow_factory, other.user_id, other.channel_account_id)
    approved = await _accept(uow_factory, other.user_id, other_attempt)
    await _ingest_done(uow_factory, approved)
    await _approve(uow_factory, approved, admin_id)

    with pytest.raises(ConflictError) as exc_approved:
        await _reject(uow_factory, approved, admin_id)
    assert exc_approved.value.code == "not_pending"
