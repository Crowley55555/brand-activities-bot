"""Фабрики фазы 3c: гость с согласием, подача Бренд B и решения модератора через ядро.

Все шаги идут через core-двери (без HTTP): тесты воркера и outbox не зависят от API фаз 3a/3b.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.activities.ugc import (
    accept_from_user,
    approve,
    ingest_file_done,
    reject,
    start_ugc,
)
from app.core.types.ids import AdminId, ChannelAccountId, UgcSubmissionId, UserId
from tests.factories import Guest, create_guest

SLUG = "brand_b_photo_subscription"
NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
POST_URL = "https://www.instagram.com/p/example/"
PROFILE_URL = "https://www.instagram.com/example/"
TG_FILE_ID = "AgACAgIAAxkBAAIBHTTP"
MIME_JPEG = "image/jpeg"
SIZE_OK = 102400


@dataclass(frozen=True)
class BrandBCase:
    guest: Guest
    attempt_id: int
    submission_id: int


async def brand_b_submission(uow_factory: Any, engine: Engine, external_id: int) -> BrandBCase:
    """Гость с согласием, открытое прохождение Бренд B и подача в receiving (задача ingest)."""
    guest = create_guest(engine, external_id=str(external_id), now=NOW)
    user = UserId(guest.user_id)
    channel = ChannelAccountId(guest.channel_account_id)
    async with uow_factory() as uow:
        r = uow.repositories
        started = await start_ugc(r.consents, r.catalog, r.attempts, user, SLUG, channel, NOW)
    async with uow_factory() as uow:
        r = uow.repositories
        accepted = await accept_from_user(
            r.consents,
            r.catalog,
            r.attempts,
            r.ugc,
            r.jobs,
            user,
            started.attempt_id,
            TG_FILE_ID,
            MIME_JPEG,
            SIZE_OK,
            POST_URL,
            PROFILE_URL,
            NOW,
        )
    return BrandBCase(guest, int(started.attempt_id), int(accepted.submission_id))


async def ingest_done(uow_factory: Any, submission_id: int) -> None:
    async with uow_factory() as uow:
        await ingest_file_done(
            uow.repositories.ugc,
            UgcSubmissionId(submission_id),
            "ugc",
            f"{submission_id}/{uuid.uuid4().hex}.jpg",
            MIME_JPEG,
            SIZE_OK,
        )


async def approve_submission(uow_factory: Any, submission_id: int, admin_id: int) -> None:
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


async def reject_submission(
    uow_factory: Any, submission_id: int, admin_id: int, reason: str = "low_quality"
) -> None:
    async with uow_factory() as uow:
        r = uow.repositories
        await reject(
            r.ugc,
            r.attempts,
            r.catalog,
            r.jobs,
            UgcSubmissionId(submission_id),
            AdminId(admin_id),
            reason,
            None,
            NOW,
        )


def jobs_of_kind(engine: Engine, kind: str) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, kind, status, last_error, payload, attempts, max_attempts "
                    "FROM jobs WHERE kind = :kind ORDER BY id"
                ),
                {"kind": kind},
            ).all()
        )


def submission_status(engine: Engine, submission_id: int) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM ugc_submissions WHERE id = :id"), {"id": submission_id}
        ).one()
        return str(row.status)


def reward_rows(engine: Engine, attempt_id: int) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, reward_type, code, title FROM rewards "
                    "WHERE attempt_id = :aid ORDER BY id"
                ),
                {"aid": attempt_id},
            ).all()
        )


def channel_unlinked(engine: Engine, guest: Guest) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT unlinked_at FROM channel_accounts WHERE id = :id"),
            {"id": guest.channel_account_id},
        ).one()
        return row.unlinked_at is not None
