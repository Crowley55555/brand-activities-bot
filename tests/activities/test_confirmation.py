"""Подарок за пост (ADR-42, ADR-43, фаза 5): T8-T10 через core-двери и настоящий Postgres.

Приёмка - ugc.accept_link (строка pending без файла, B2/U2). Решения approve/reject - свои,
в core.activities.confirmation; UGC-подфаза для Бренд B не меняется.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from app.common.errors import ValidationError
from app.core.activities.confirmation import accept_confirmation, approve, reject
from app.core.types.ids import AdminId, ChannelAccountId, UgcSubmissionId, UserId
from app.core.types.results import UgcAcceptResult
from tests.factories import create_guest
from tests.factories.brand_d import (
    CONFIRMATION_LIMITS,
    BrandDActivity,
    attempt_status,
    make_activity,
)
from tests.factories.ugc import create_admin, job_rows, reward_rows, ugc_rows

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
POST_URL = "https://www.instagram.com/p/brand-d-example/"


async def accept_as(
    uow_factory: Any, guest: Any, activity: BrandDActivity, post_url: str = POST_URL
) -> UgcAcceptResult:
    async with uow_factory() as uow:
        repos = uow.repositories
        return await accept_confirmation(
            repos.consents,
            repos.catalog,
            repos.attempts,
            repos.ugc,
            UserId(guest.user_id),
            activity.slug,
            ChannelAccountId(guest.channel_account_id),
            post_url,
            NOW,
        )


async def approve_as(uow_factory: Any, submission_id: int, admin_id: int) -> Any:
    async with uow_factory() as uow:
        repos = uow.repositories
        return await approve(
            repos.attempts,
            repos.catalog,
            repos.rewards,
            repos.ugc,
            repos.jobs,
            UgcSubmissionId(submission_id),
            AdminId(admin_id),
            NOW,
        )


async def reject_as(uow_factory: Any, submission_id: int, admin_id: int, reason: str) -> Any:
    async with uow_factory() as uow:
        repos = uow.repositories
        return await reject(
            repos.attempts,
            repos.catalog,
            repos.ugc,
            repos.jobs,
            UgcSubmissionId(submission_id),
            AdminId(admin_id),
            reason,
            NOW,
        )


async def test_accept_confirmation_creates_pending_without_file(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T8: строка pending, file_bucket IS NULL, post_url задан; ingest не ставится, прохождение на
    модерации."""
    guest = create_guest(sync_engine, external_id="3201", now=NOW)
    activity = make_activity(sync_engine, "confirmation", CONFIRMATION_LIMITS)
    accepted = await accept_as(uow_factory, guest, activity)

    assert accepted.status == "pending"
    rows = ugc_rows(sync_engine, accepted.attempt_id)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].file_bucket is None
    assert rows[0].file_key is None
    assert rows[0].post_url == POST_URL
    assert job_rows(sync_engine, "ingest_ugc_file") == []
    assert attempt_status(sync_engine, accepted.attempt_id) == "pending_moderation"


async def test_approve_confirmation_grants_promocode(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T9: approve -> approved, одна награда promocode BRAND_D-XXXXXXXX, прохождение completed."""
    guest = create_guest(sync_engine, external_id="3202", now=NOW)
    activity = make_activity(sync_engine, "confirmation", CONFIRMATION_LIMITS)
    accepted = await accept_as(uow_factory, guest, activity)
    admin_id = create_admin(sync_engine)

    decided = await approve_as(uow_factory, accepted.submission_id, admin_id)

    assert decided.status == "approved"
    rewards = reward_rows(sync_engine, accepted.attempt_id)
    assert [row.reward_type for row in rewards] == ["promocode"]
    assert re.fullmatch(r"BRAND_D-[A-Z0-9]{8}", rewards[0].code)
    assert attempt_status(sync_engine, accepted.attempt_id) == "completed"
    notify = job_rows(sync_engine, "notify_promocode")
    assert [int(job.payload["reward_id"]) for job in notify] == [int(rewards[0].id)]


async def test_reject_confirmation_with_reason(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """T10: reject с слагом из limits -> rejected, прохождение сразу rejected, уведомление с
    причиной."""
    guest = create_guest(sync_engine, external_id="3203", now=NOW)
    activity = make_activity(sync_engine, "confirmation", CONFIRMATION_LIMITS)
    accepted = await accept_as(uow_factory, guest, activity)
    admin_id = create_admin(sync_engine)

    with pytest.raises(ValidationError) as unknown:
        await reject_as(uow_factory, accepted.submission_id, admin_id, "no_such_reason")
    assert unknown.value.code == "reason_slug_unknown"

    decided = await reject_as(uow_factory, accepted.submission_id, admin_id, "not_posted")

    assert decided.status == "rejected"
    assert decided.rejection_reason_slug == "not_posted"
    assert attempt_status(sync_engine, accepted.attempt_id) == "rejected"
    notify = job_rows(sync_engine, "notify_confirmation_rejected")
    assert len(notify) == 1
    assert notify[0].payload["reason_slug"] == "not_posted"
    assert int(notify[0].payload["attempt_id"]) == accepted.attempt_id
