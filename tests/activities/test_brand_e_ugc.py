"""Приёмочный тест фазы 6: Бренд E «Фото аромата» (T3): фото -> модерация -> только промокод.

Идём через настоящее ядро UGC и настоящий Postgres (как test_brand_b_ugc_core.py). Slug и limits
активности - из сид-миграции 0015 (правило 5); здесь это только тестовые данные.

У Бренд E нет ваучера (`voucher_title` = null, решение B1 ревью фазы 6): approve выдаёт один
промокод BRAND_E-XXXXXXXX и ставит notify_promocode, а не notify_reward.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from app.core.activities.ugc import accept_from_user, approve, ingest_file_done, start_ugc
from app.core.types.ids import AdminId, AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from tests.factories import Guest, create_guest
from tests.factories.survey import UowFactory
from tests.factories.ugc import attempt_status, create_admin, job_rows, reward_rows, ugc_rows

SLUG = "brand_e_photo"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
TG_FILE_ID = "AgACAgIAAxkBAAIBBRAND_EFILE"
MIME_JPEG = "image/jpeg"
SIZE_OK = 102400
PROMO_RE = re.compile(r"^BRAND_E-[A-Z0-9]{8}$")


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


async def _accept(uow_factory: UowFactory, guest: Guest, attempt_id: int) -> int:
    """Фото без ссылки на пост: у Бренд E модерируется только фото (ADR-44)."""
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
            MIME_JPEG,
            SIZE_OK,
            None,
            None,
            NOW,
        )
    return int(accepted.submission_id)


async def _ingest_done(uow_factory: UowFactory, submission_id: int) -> None:
    """Имитирует воркер ingest (фаза 3c): файл лёг в хранилище."""
    async with uow_factory() as uow:
        await ingest_file_done(
            uow.repositories.ugc,
            UgcSubmissionId(submission_id),
            "ugc",
            f"{uuid.uuid4()}.jpg",
            MIME_JPEG,
            SIZE_OK,
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


# ---------------------------------------------------------------- T3


async def test_brand_e_ugc_accept_and_approve_grants_promocode(
    uow_factory: UowFactory, sync_engine: Engine
) -> None:
    """T3: accept -> ingest -> pending -> approve: одна награда (промокод), без ваучера."""
    guest = create_guest(sync_engine)
    attempt_id = await _start(uow_factory, guest)
    submission_id = await _accept(uow_factory, guest, attempt_id)
    await _ingest_done(uow_factory, submission_id)
    admin_id = create_admin(sync_engine)

    await _approve(uow_factory, submission_id, admin_id)

    row = ugc_rows(sync_engine, attempt_id)[0]
    assert row.status == "approved"
    assert row.decided_by_admin_id == admin_id

    rewards = reward_rows(sync_engine, attempt_id)
    assert [reward.reward_type for reward in rewards] == ["promocode"]
    assert PROMO_RE.fullmatch(rewards[0].code), rewards[0].code
    # Название - display_name активности: без него уведомление печатает «: ваш промокод ...».
    assert rewards[0].title == "Фото аромата"
    assert attempt_status(sync_engine, attempt_id) == "completed"

    notify = job_rows(sync_engine, "notify_promocode")
    assert [int(job.payload["reward_id"]) for job in notify] == [int(rewards[0].id)]
    assert job_rows(sync_engine, "notify_reward") == []
