"""Подарок за пост (ADR-42, ADR-43, фаза 5): ссылка без фото; решения - свои, не UGC.

Таблица ugc_submissions и её state machine переиспользуются (pending -> approved | rejected),
приёмка -
ugc.accept_link (строка pending без файла, U2). approve выдаёт промокод по шаблону
limits.promocode_template. reject закрывает прохождение сразу: повтора нет (B2). Подфаза
approve/reject
для Бренд B (ugc.approve/reject) не меняется.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from app.common.errors import ConflictError, NotFound, ValidationError
from app.core.activities.ugc.repository import UGCRepo
from app.core.activities.ugc.service import accept_link
from app.core.activities.ugc.state_machine import can_transition
from app.core.attempts import AttemptRepo, close_from_moderation, get_attempt
from app.core.catalog import CatalogRepo, get_activity_by_id
from app.core.consent import ConsentRepo, require_consent
from app.core.jobs import JobRepo, enqueue
from app.core.rewards import RewardRepo, grant_on_completion
from app.core.types.dto import GrantSpec, UgcSubmissionRef
from app.core.types.ids import AdminId, ChannelAccountId, UgcSubmissionId, UserId
from app.core.types.results import UgcAcceptResult

ACTIVITY_KIND: Final = "confirmation"
_REASON_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{1,63}")


def _limit_text(limits: Mapping[str, Any], key: str) -> str:
    value = limits.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError("activity_limits_invalid")
    return value


def _limit_reasons(limits: Mapping[str, Any]) -> Mapping[str, str]:
    value = limits.get("reject_reasons")
    if not isinstance(value, dict) or not value:
        raise ValidationError("activity_limits_invalid")
    return {str(slug): str(label) for slug, label in value.items()}


async def _require_submission(ugc: UGCRepo, submission_id: UgcSubmissionId) -> UgcSubmissionRef:
    submission = await ugc.get(submission_id)
    if submission is None:
        raise NotFound("ugc_not_found")
    return submission


async def accept_confirmation(
    consents: ConsentRepo,
    catalog: CatalogRepo,
    attempts: AttemptRepo,
    ugc: UGCRepo,
    user_id: UserId,
    activity_slug: str,
    channel_account_id: ChannelAccountId,
    post_url: str,
    now: datetime,
) -> UgcAcceptResult:
    """Ссылка на пост (B2): правило 1 первой строкой, дальше ugc.accept_link (U2)."""
    await require_consent(consents, user_id)
    return await accept_link(
        consents,
        catalog,
        attempts,
        ugc,
        user_id,
        activity_slug,
        channel_account_id,
        post_url,
        now,
    )


async def approve(
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    rewards: RewardRepo,
    ugc: UGCRepo,
    jobs: JobRepo,
    submission_id: UgcSubmissionId,
    admin_id: AdminId,
    now: datetime,
) -> UgcSubmissionRef:
    """Решение «одобрено»: pending -> approved, прохождение completed, промокод и уведомление."""
    current = await _require_submission(ugc, submission_id)
    if not can_transition(current.status, "approved"):
        raise ConflictError("not_pending")
    attempt = await get_attempt(attempts, current.attempt_id)
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    if activity.kind != ACTIVITY_KIND:
        raise ValidationError("activity_kind_mismatch")
    template = _limit_text(activity.limits, "promocode_template")
    decided = await ugc.set_approved(submission_id, admin_id, now)
    if decided is None:
        raise ConflictError("not_pending")
    await close_from_moderation(attempts, current.attempt_id, "completed", None, now)
    issued = await grant_on_completion(
        rewards,
        current.attempt_id,
        [GrantSpec(reward_type="promocode", title=activity.display_name, code_template=template)],
        now,
    )
    await enqueue(
        jobs,
        "notify_promocode",
        {"reward_id": int(issued[0].id)},
        run_after=now,
        idempotency_key=f"notify_promocode:{int(submission_id)}",
    )
    return decided


async def reject(
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    ugc: UGCRepo,
    jobs: JobRepo,
    submission_id: UgcSubmissionId,
    admin_id: AdminId,
    reason_slug: str,
    now: datetime,
) -> UgcSubmissionRef:
    """Решение «отказ»: pending -> rejected, прохождение rejected сразу (B2, без повтора)."""
    if not _REASON_SLUG_RE.fullmatch(reason_slug):
        raise ValidationError("reason_slug_invalid")
    current = await _require_submission(ugc, submission_id)
    if not can_transition(current.status, "rejected"):
        raise ConflictError("not_pending")
    attempt = await get_attempt(attempts, current.attempt_id)
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    if activity.kind != ACTIVITY_KIND:
        raise ValidationError("activity_kind_mismatch")
    if reason_slug not in _limit_reasons(activity.limits):
        raise ValidationError("reason_slug_unknown")
    decided = await ugc.set_rejected(submission_id, admin_id, reason_slug, None, now)
    if decided is None:
        raise ConflictError("not_pending")
    await close_from_moderation(attempts, current.attempt_id, "rejected", reason_slug, now)
    await enqueue(
        jobs,
        "notify_confirmation_rejected",
        {
            "user_id": int(attempt.user_id),
            "reason_slug": reason_slug,
            "attempt_id": int(current.attempt_id),
        },
        run_after=now,
        idempotency_key=f"confirmation_rejected:{int(submission_id)}",
    )
    return decided
