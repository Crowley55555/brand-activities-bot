"""Use-case'ы Бренд B UGC (фаза 3a, дополнено фазой 3b): приёмка фото, повтор, ingest, решение.

Фаза A приёмки (accept_from_user, resubmit) - синхронно, из HTTP. Фаза B (ingest_file_done и
ingest_file_failed) - из воркера фазы 3c; до тех пор её зовут тесты. Решения модератора -
approve и reject. Отказ не закрывает прохождение: гость может повторить новым фото (N1).
Исчерпание лимита повторов - результат ResubmitResult, а не исключение (N4). Сбой загрузки
(ingest_failed) тоже не отменяет прохождение и считается в лимит повторов (F4).

Фаза 3b: MIME и размер проверяются по декларации из Telegram на приёмке (Q1, ADR-23); решение
модератора ставит уведомления notify_reward (с ваучером) или notify_promocode (без ваучера, B1
фазы 6) и notify_ugc_rejected с payload из ADR-25 (Q2, Q3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from app.common.errors import ConflictError, NotFound, ValidationError
from app.core.activities.ugc.repository import UGCRepo
from app.core.activities.ugc.state_machine import can_transition
from app.core.attempts import (
    AttemptRepo,
    close_from_moderation,
    get_attempt,
    get_owned_attempt,
    mark_pending_moderation,
    start_attempt,
)
from app.core.catalog import CatalogRepo, get_activity_by_id
from app.core.consent import ConsentRepo, require_consent
from app.core.jobs import JobRepo, enqueue
from app.core.rewards import RewardRepo, grant_on_completion
from app.core.types.dto import ActivityRef, GrantSpec, UgcSubmissionRef
from app.core.types.enums import UgcStatus
from app.core.types.ids import AdminId, AttemptId, ChannelAccountId, UgcSubmissionId, UserId
from app.core.types.results import ResubmitResult, UgcAcceptResult, UgcStartResult

ACTIVITY_KIND: Final = "ugc"
# Строка с таким статусом уже принята (фото есть или ждёт модерации): повтор недопустим.
_ACTIVE_STATUSES: Final[frozenset[UgcStatus]] = frozenset({"receiving", "pending", "approved"})
_REASON_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{1,63}")
# Причина сбоя загрузки - только слаг из воркера (F3): свободный текст в core не пишем.
_INGEST_REASON_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]{1,32}$")
_NOTE_MAX_LENGTH: Final[int] = 500


def _limit_text(limits: Mapping[str, Any], key: str) -> str:
    """Строковый ключ из activities.limits (правило 5: шаблоны и названия - из данных)."""
    value = limits.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError("activity_limits_invalid")
    return value


def _limit_int(limits: Mapping[str, Any], key: str) -> int:
    """Неотрицательное целое из activities.limits (например, max_resubmits)."""
    value = limits.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("activity_limits_invalid")
    return value


def _limit_str_list(limits: Mapping[str, Any], key: str) -> list[str]:
    """Непустой список строк из activities.limits (allowed_mimes)."""
    value = limits.get(key)
    if not isinstance(value, list) or not value:
        raise ValidationError("activity_limits_invalid")
    if not all(isinstance(item, str) and item for item in value):
        raise ValidationError("activity_limits_invalid")
    return [str(item) for item in value]


def _limit_reject_reasons(limits: Mapping[str, Any]) -> Mapping[str, str]:
    """Причины отказа activities.limits.reject_reasons: слаг -> подпись (Q4, правило 5)."""
    value = limits.get("reject_reasons")
    if not isinstance(value, dict) or not value:
        raise ValidationError("activity_limits_invalid")
    reasons: dict[str, str] = {}
    for slug, label in value.items():
        if not isinstance(slug, str) or not isinstance(label, str) or not label:
            raise ValidationError("activity_limits_invalid")
        reasons[slug] = label
    return reasons


def _require_file_id(tg_file_id: str) -> None:
    if not tg_file_id.strip():
        raise ValidationError("tg_file_id_required")


def _check_declared_file(limits: Mapping[str, Any], mime_type: str, file_size_bytes: int) -> None:
    """Декларация из Telegram против лимитов активности (Q1): сначала MIME, затем размер.

    Реальные байты на приёмке недоступны: accept синхронный (04 §4.6), файл качает воркер.
    Проверка байтов (python-magic) - в ingest фазы 3c; расхождение там даёт ingest_failed
    с причиной mime_mismatch.
    """
    if file_size_bytes <= 0:
        raise ValidationError("file_size_invalid")
    if mime_type not in _limit_str_list(limits, "allowed_mimes"):
        raise ValidationError("mime_not_allowed")
    if file_size_bytes > _limit_int(limits, "max_file_bytes"):
        raise ValidationError("file_too_large")


async def _require_submission(ugc: UGCRepo, submission_id: UgcSubmissionId) -> UgcSubmissionRef:
    submission = await ugc.get(submission_id)
    if submission is None:
        raise NotFound("ugc_not_found")
    return submission


async def _closed_after_decision(
    ugc: UGCRepo, limits: Mapping[str, Any], attempt_id: AttemptId
) -> bool:
    """Повтора после текущего решения не будет (Q3): строк прохождения уже max_resubmits + 1.

    Считаем все строки, включая ingest_failed (F4). Совпадает с правилом resubmit (N4).
    """
    max_resubmits = _limit_int(limits, "max_resubmits")
    submissions = await ugc.list_for_attempt(attempt_id)
    return len(submissions) >= max_resubmits + 1


async def _open_receiving(
    ugc: UGCRepo,
    jobs: JobRepo,
    attempt_id: AttemptId,
    user_id: UserId,
    tg_file_id: str,
    post_url: str | None,
    subscription_url: str | None,
    now: datetime,
) -> UgcAcceptResult:
    """Строка receiving и задача ingest_ugc_file: общая часть accept_from_user и resubmit."""
    submission = await ugc.insert_receiving(attempt_id, post_url, subscription_url, now)
    await enqueue(
        jobs,
        "ingest_ugc_file",
        {
            "submission_id": int(submission.id),
            "tg_file_id": tg_file_id,
            "user_id": int(user_id),
        },
        idempotency_key=f"ugc_ingest:{int(submission.id)}",
    )
    return UgcAcceptResult(
        submission_id=submission.id,
        attempt_id=attempt_id,
        status=submission.status,
    )


async def start_ugc(
    consents: ConsentRepo,
    catalog: CatalogRepo,
    attempts: AttemptRepo,
    user_id: UserId,
    activity_slug: str,
    channel_account_id: ChannelAccountId,
    now: datetime,
) -> UgcStartResult:
    """Старт UGC: прохождение открыто (in_progress), гостю просят фото (`ask_photo`)."""
    started = await start_attempt(
        consents,
        catalog,
        attempts,
        user_id,
        activity_slug,
        channel_account_id,
        now,
        expected_kind=ACTIVITY_KIND,
    )
    return UgcStartResult(
        attempt_id=started.attempt_id,
        activity_slug=started.activity_slug,
        next_step="ask_photo",
    )


async def accept_from_user(
    consents: ConsentRepo,
    catalog: CatalogRepo,
    attempts: AttemptRepo,
    ugc: UGCRepo,
    jobs: JobRepo,
    user_id: UserId,
    attempt_id: AttemptId,
    tg_file_id: str,
    mime_type: str,
    file_size_bytes: int,
    post_url: str | None,
    subscription_url: str | None,
    now: datetime,
) -> UgcAcceptResult:
    """Фаза A приёмки (ADR-16, 04 §4.6): фото принято, файл докачает фоновая задача.

    Правило 1: согласие проверяется первой строкой. Дальше: владелец прохождения, активность
    kind='ugc', декларация MIME и размера против лимитов (Q1), переход прохождения
    in_progress -> pending_moderation, строка receiving и задача ingest_ugc_file с ключом
    ugc_ingest:<id>. Ошибка декларации - до любой записи.
    """
    await require_consent(consents, user_id)
    attempt = await get_owned_attempt(attempts, attempt_id, user_id)
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    if activity.kind != ACTIVITY_KIND:
        raise ValidationError("activity_kind_mismatch")
    _require_file_id(tg_file_id)
    _check_declared_file(activity.limits, mime_type, file_size_bytes)

    await mark_pending_moderation(attempts, attempt_id)
    return await _open_receiving(
        ugc, jobs, attempt_id, user_id, tg_file_id, post_url, subscription_url, now
    )


CONFIRMATION_KIND: Final = "confirmation"
_POST_URL_MAX_LENGTH: Final[int] = 2048


def _require_post_url(post_url: str) -> None:
    """Ссылка на пост: http(s) и не длиннее лимита (подарок за пост, ADR-42)."""
    if not post_url.startswith(("https://", "http://")) or len(post_url) > _POST_URL_MAX_LENGTH:
        raise ValidationError("post_url_invalid")


async def accept_link(
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
    """Приёмка ссылки без фото (ADR-42, U2): строка pending без файла, прохождение на модерации.

    Правило 1: согласие первой строкой. Прохождение открывает start_attempt с kind='confirmation'
    (once-per-day держит БД), затем in_progress -> pending_moderation и строка pending. Задачи
    ingest не ставится: файла нет.
    """
    await require_consent(consents, user_id)
    _require_post_url(post_url)
    started = await start_attempt(
        consents,
        catalog,
        attempts,
        user_id,
        activity_slug,
        channel_account_id,
        now,
        expected_kind=CONFIRMATION_KIND,
    )
    await mark_pending_moderation(attempts, started.attempt_id)
    submission = await ugc.insert_pending_link(started.attempt_id, post_url, now)
    return UgcAcceptResult(
        submission_id=submission.id,
        attempt_id=started.attempt_id,
        status=submission.status,
    )


async def resubmit(
    consents: ConsentRepo,
    catalog: CatalogRepo,
    attempts: AttemptRepo,
    ugc: UGCRepo,
    jobs: JobRepo,
    user_id: UserId,
    attempt_id: AttemptId,
    tg_file_id: str,
    mime_type: str,
    file_size_bytes: int,
    post_url: str | None,
    subscription_url: str | None,
    now: datetime,
) -> ResubmitResult:
    """Повтор после отказа (N2): новая строка receiving на том же прохождении и задача ingest.

    Порядок (F1, ревью фазы 3a): блокировка строки прохождения первой - параллельные повторы
    встают в очередь; согласие до любой записи (правило 1); владелец через get_owned_attempt.
    Прохождение должно быть на модерации; строк receiving/pending/approved быть не должно
    (допущение: повтор возможен только после отказа или сбоя загрузки). Все строки прохождения
    считаются в лимит (F4), включая ingest_failed; строк не больше max_resubmits + 1.

    N4: исчерпание лимита - результат `limit_exceeded`, а не исключение. Прохождение закрывается
    rejected в этой же транзакции, UoW коммитит при нормальном выходе. Гонка на уровне БД
    закрыта частичным unique-индексом ugc_submission_active (миграция 0009): конфликт
    превращается в ConflictError("submission_active") в репозитории.
    """
    await attempts.lock_for_update(attempt_id)
    await require_consent(consents, user_id)
    attempt = await get_owned_attempt(attempts, attempt_id, user_id)
    if attempt.status != "pending_moderation":
        raise ConflictError("attempt_not_pending_moderation")
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    if activity.kind != ACTIVITY_KIND:
        raise ValidationError("activity_kind_mismatch")
    _require_file_id(tg_file_id)
    _check_declared_file(activity.limits, mime_type, file_size_bytes)
    max_resubmits = _limit_int(activity.limits, "max_resubmits")

    submissions = await ugc.list_for_attempt(attempt_id)
    if any(item.status in _ACTIVE_STATUSES for item in submissions):
        raise ConflictError("submission_active")
    if len(submissions) >= max_resubmits + 1:
        await close_from_moderation(attempts, attempt_id, "rejected", "resubmit_limit", now)
        return ResubmitResult(kind="limit_exceeded", attempt_closed=True)
    opened = await _open_receiving(
        ugc, jobs, attempt_id, user_id, tg_file_id, post_url, subscription_url, now
    )
    return ResubmitResult(kind="success", submission_id=opened.submission_id)


async def ingest_file_done(
    ugc: UGCRepo,
    submission_id: UgcSubmissionId,
    bucket: str,
    key: str,
    content_type: str,
    size_bytes: int,
) -> UgcSubmissionRef:
    """Фаза B приёмки (воркер, 3c): файл лёг в хранилище, receiving -> pending (04 §4.6)."""
    if size_bytes <= 0 or not bucket or not key or not content_type:
        raise ValidationError("ingest_metadata_invalid")
    ingested = await ugc.set_pending_after_ingest(
        submission_id, bucket, key, content_type, size_bytes
    )
    if ingested is not None:
        return ingested
    await _require_submission(ugc, submission_id)
    raise ConflictError("not_receiving")


async def ingest_file_failed(
    ugc: UGCRepo,
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    jobs: JobRepo,
    submission_id: UgcSubmissionId,
    reason: str,
    now: datetime,
    *,
    notify_system: bool = True,
) -> UgcSubmissionRef:
    """Загрузка не удалась (F4, пересмотр N3): receiving -> ingest_failed.

    Прохождение остаётся pending_moderation: оно не отменяется. При notify_system=True ставится
    NOTIFY_INGEST_FAILED (kind notify_ingest_failed, ТОП-1 ревью 3c) с системной причиной и
    attempt_closed; гость получает техническую подпись, а не «работа отклонена». Отзыв согласия
    передаёт notify_system=False: пользователь сам отозвал согласие, спам не нужен (решение 5).
    ingest_failed считается в max_resubmits. Причина - слаг (`_INGEST_REASON_RE`).
    """
    if not _INGEST_REASON_RE.fullmatch(reason):
        raise ValidationError("reason_invalid_slug")
    failed = await ugc.set_ingest_failed(submission_id)
    if failed is None:
        await _require_submission(ugc, submission_id)
        raise ConflictError("not_receiving")
    if not notify_system:
        return failed
    attempt = await get_attempt(attempts, failed.attempt_id)
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    closed = await _closed_after_decision(ugc, activity.limits, failed.attempt_id)
    await enqueue(
        jobs,
        "notify_ingest_failed",
        {
            "user_id": int(attempt.user_id),
            "attempt_id": int(failed.attempt_id),
            "system_reason_slug": reason,
            "attempt_closed": closed,
        },
        run_after=now,
        idempotency_key=f"ingest_failed:{int(submission_id)}",
    )
    return failed


_VOUCHER_LIMIT_KEYS: Final[tuple[str, ...]] = ("voucher_title", "voucher_code_template")


def _has_voucher(limits: Mapping[str, Any]) -> bool:
    """Ваучер настроен, если задан хотя бы один его ключ (B1 ревью фазы 6).

    Настроенный ваучер требует оба ключа: `_limit_text` в `_approve_grants` бросит ошибку
    конфигурации. Без ваучера (`voucher_title` = null) approve выдаёт только промокод.
    """
    return any(limits.get(key) is not None for key in _VOUCHER_LIMIT_KEYS)


def _approve_grants(activity: ActivityRef) -> list[GrantSpec]:
    """Награды одобрения: ваучер (если настроен) и промокод, в этом порядке.

    Название промокода задаётся только без ваучера: оно нужно уведомлению notify_promocode.
    С ваучером промокод выдаётся без названия.
    """
    limits = activity.limits
    has_voucher = _has_voucher(limits)
    grants: list[GrantSpec] = []
    if has_voucher:
        grants.append(
            GrantSpec(
                reward_type="voucher",
                title=_limit_text(limits, "voucher_title"),
                code_template=_limit_text(limits, "voucher_code_template"),
            )
        )
    grants.append(
        GrantSpec(
            reward_type="promocode",
            title=None if has_voucher else activity.display_name,
            code_template=_limit_text(limits, "promocode_template"),
        )
    )
    return grants


async def approve(
    ugc: UGCRepo,
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    rewards: RewardRepo,
    jobs: JobRepo,
    submission_id: UgcSubmissionId,
    admin_id: AdminId,
    now: datetime,
) -> UgcSubmissionRef:
    """Решение «одобрено»: pending -> approved, прохождение completed, награды - одной транзакцией.

    Награды: промокод и, если ваучер настроен, ваучер «напиток» (название и код - из
    activities.limits). С ваучером задача notify_reward (Q2) несёт reward_id ваучера; воркер (3c)
    по нему найдёт все награды прохождения. Без ваучера (`voucher_title` = null, B1 ревью фазы 6)
    ставится notify_promocode с reward_id промокода (шаблон promocode из фазы 5).
    Переход проверяет state_machine.can_transition; вторая попытка одобрить ту же строку -
    409 not_pending.
    """
    current = await _require_submission(ugc, submission_id)
    if not can_transition(current.status, "approved"):
        raise ConflictError("not_pending")
    attempt = await get_attempt(attempts, current.attempt_id)
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    grants = _approve_grants(activity)
    decided = await ugc.set_approved(submission_id, admin_id, now)
    if decided is None:
        raise ConflictError("not_pending")
    await close_from_moderation(attempts, current.attempt_id, "completed", None, now)
    issued = await grant_on_completion(rewards, current.attempt_id, grants, now)
    if _has_voucher(activity.limits):
        voucher = next((reward for reward in issued if reward.reward_type == "voucher"), None)
        if voucher is None:
            raise RuntimeError("voucher_not_issued")
        await enqueue(
            jobs,
            "notify_reward",
            {"reward_id": int(voucher.id)},
            run_after=now,
            idempotency_key=f"notify_reward:{int(submission_id)}",
        )
    else:
        promo = next((reward for reward in issued if reward.reward_type == "promocode"), None)
        if promo is None:
            raise RuntimeError("promocode_not_issued")
        await enqueue(
            jobs,
            "notify_promocode",
            {"reward_id": int(promo.id)},
            run_after=now,
            idempotency_key=f"notify_promocode:{int(submission_id)}",
        )
    return decided


async def reject(
    ugc: UGCRepo,
    attempts: AttemptRepo,
    catalog: CatalogRepo,
    jobs: JobRepo,
    submission_id: UgcSubmissionId,
    admin_id: AdminId,
    reason_slug: str,
    note: str | None,
    now: datetime,
) -> UgcSubmissionRef:
    """Решение «отказ» (N1): pending -> rejected. Прохождение остаётся pending_moderation.

    Причина - один из слагов activities.limits.reject_reasons (Q4); чужой слаг - 422
    reason_slug_unknown. Гость получает уведомление notify_ugc_rejected с attempt_closed (Q3):
    true, если повтора уже не будет. Прохождение закрывается rejected при исчерпании лимита
    повторов (N4, см. resubmit). Отказ по ingest_failed или approved - 409 not_pending (F7).
    """
    if not _REASON_SLUG_RE.fullmatch(reason_slug):
        raise ValidationError("reason_slug_invalid")
    if note is not None and len(note) > _NOTE_MAX_LENGTH:
        raise ValidationError("note_too_long")
    current = await _require_submission(ugc, submission_id)
    if not can_transition(current.status, "rejected"):
        raise ConflictError("not_pending")
    attempt = await get_attempt(attempts, current.attempt_id)
    activity = await get_activity_by_id(catalog, attempt.activity_id)
    if reason_slug not in _limit_reject_reasons(activity.limits):
        raise ValidationError("reason_slug_unknown")
    decided = await ugc.set_rejected(submission_id, admin_id, reason_slug, note, now)
    if decided is None:
        raise ConflictError("not_pending")
    closed = await _closed_after_decision(ugc, activity.limits, current.attempt_id)
    await enqueue(
        jobs,
        "notify_ugc_rejected",
        {
            "user_id": int(attempt.user_id),
            "reason_slug": reason_slug,
            "attempt_closed": closed,
            "attempt_id": int(attempt.id),
        },
        run_after=now,
        idempotency_key=f"ugc_rejected:{int(submission_id)}",
    )
    return decided
