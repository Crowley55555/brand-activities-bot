from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.api.deps import get_clock, get_uow
from app.api.middleware.auth_internal import verify_internal_hmac
from app.api.schemas import (
    ActivityAnswerIn,
    ActivityAnswerOut,
    ActivityFinishIn,
    ActivityFinishOut,
    ActivityStartIn,
    ActivityStartOut,
    NextStepOut,
    ProductCardOut,
    RewardOut,
    SurveyOptionOut,
    SurveyQuestionOut,
    UgcAcceptIn,
    UgcAcceptOut,
    UgcResubmitIn,
    UgcResubmitOut,
)
from app.common.errors import NotFound, ValidationError
from app.core.activities.survey import finish_survey, start_survey, submit_answer
from app.core.activities.ugc import accept_from_user, resubmit, start_ugc
from app.core.attempts import AttemptRepo, get_owned_attempt
from app.core.catalog import get_activity_by_slug
from app.core.channels import lookup_user
from app.core.channels.repository import ChannelAccountRepo
from app.core.ports.clock import ClockPort
from app.core.types.dto import SurveyQuestionRef
from app.core.types.enums import ChannelType
from app.core.types.ids import AttemptId, SurveyQuestionId
from app.core.types.results import UgcStartResult
from app.db.unit_of_work import UnitOfWork

logger = logging.getLogger("app.api.internal.activities")

router = APIRouter(prefix="/internal/v1/activities")


def _parse[ModelT: BaseModel](model: type[ModelT], body: bytes) -> ModelT:
    """Тело запроса -> модель; кривой JSON/поля дают 422 bad_request, а не 500."""
    try:
        return model.model_validate_json(body)
    except PydanticValidationError as exc:
        raise ValidationError("bad_request") from exc


def _question_out(question: SurveyQuestionRef | None) -> SurveyQuestionOut | None:
    if question is None:
        return None
    return SurveyQuestionOut(
        id=int(question.id),
        position=question.position,
        text=question.text,
        question_type=question.question_type,
        options=[SurveyOptionOut(key=o.key, label=o.label) for o in question.options],
    )


async def _owned_attempt(
    channels: ChannelAccountRepo,
    attempts: AttemptRepo,
    channel_type: ChannelType,
    external_id: str,
    attempt_id: int,
) -> AttemptId:
    """Граница канала: отправитель известен и владеет прохождением.

    Нет канала -> 404 channel_not_found. Чужое прохождение -> 403 attempt_not_owned:
    без проверки гость с валидным HMAC закрыл бы или испортил чужой опрос (ревью, S1).
    """
    channel = await lookup_user(channels, channel_type, external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    attempt = await get_owned_attempt(attempts, AttemptId(attempt_id), channel.user_id)
    return attempt.id


def _ugc_start_out(result: UgcStartResult) -> ActivityStartOut:
    return ActivityStartOut(
        status="ok",
        attempt_id=int(result.attempt_id),
        activity_slug=result.activity_slug,
        kind="ugc",
        total_questions=0,
        question=None,
        next_step=NextStepOut(kind=result.next_step),
    )


@router.post("/start", response_model=ActivityStartOut)
async def start_activity(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> ActivityStartOut:
    payload = _parse(ActivityStartIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")

    # Диспетчер по activities.kind: ugc - фото (фаза 3a), остальное - опрос (фаза 2).
    activity = await get_activity_by_slug(r.catalog, payload.activity_slug)
    if activity.kind == "ugc":
        started = await start_ugc(
            r.consents,
            r.catalog,
            r.attempts,
            channel.user_id,
            payload.activity_slug,
            channel.id,
            clock.now(),
        )
        logger.info(
            "activity_started",
            extra={
                "event": "activity_started",
                "user_id": int(channel.user_id),
                "attempt_id": int(started.attempt_id),
                "activity_slug": started.activity_slug,
            },
        )
        await uow.commit()  # F2: ответ 2xx уходит только после фиксации
        return _ugc_start_out(started)

    # Чужой kind для опроса даёт 422 activity_kind_mismatch (проверяет start_survey).
    result = await start_survey(
        r.consents,
        r.attempts,
        r.catalog,
        channel.user_id,
        channel.id,
        payload.activity_slug,
        clock.now(),
    )
    logger.info(
        "activity_started",
        extra={
            "event": "activity_started",
            "user_id": int(channel.user_id),
            "attempt_id": int(result.attempt_id),
            "activity_slug": result.activity_slug,
        },
    )
    await uow.commit()  # F2: ответ 2xx уходит только после фиксации
    return ActivityStartOut(
        status="ok",
        attempt_id=int(result.attempt_id),
        activity_slug=result.activity_slug,
        kind=result.kind,
        total_questions=result.total_questions,
        question=_question_out(result.next_question),
    )


@router.post("/answer", response_model=ActivityAnswerOut)
async def answer_activity(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> ActivityAnswerOut:
    payload = _parse(ActivityAnswerIn, body)
    r = uow.repositories
    attempt_id = await _owned_attempt(
        r.channels, r.attempts, payload.channel_type, payload.external_id, payload.attempt_id
    )
    result = await submit_answer(
        r.consents,
        r.attempts,
        r.catalog,
        r.surveys,
        attempt_id,
        SurveyQuestionId(payload.question_id),
        payload.selected_option_keys,
        payload.free_text,
        clock.now(),
    )
    logger.info(
        "activity_answered",
        extra={
            "event": "activity_answered",
            "attempt_id": int(result.attempt_id),
            "question_id": int(result.question_id),
            "answered_count": result.answered_count,
        },
    )
    await uow.commit()  # F2
    return ActivityAnswerOut(
        status="ok",
        attempt_id=int(result.attempt_id),
        is_last=result.is_last,
        answered_count=result.answered_count,
        total_questions=result.total_questions,
        next_question=_question_out(result.next_question),
    )


@router.post("/finish", response_model=ActivityFinishOut)
async def finish_activity(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> ActivityFinishOut:
    payload = _parse(ActivityFinishIn, body)
    r = uow.repositories
    attempt_id = await _owned_attempt(
        r.channels, r.attempts, payload.channel_type, payload.external_id, payload.attempt_id
    )
    result = await finish_survey(
        r.consents,
        r.attempts,
        r.catalog,
        r.surveys,
        r.rewards,
        attempt_id,
        clock.now(),
    )
    # Сам промокод в лог не пишем: это предъявительский код. Только факт и счётчики.
    logger.info(
        "activity_finished",
        extra={
            "event": "activity_finished",
            "attempt_id": int(result.attempt_id),
            "recommendations": len(result.recommendations),
            "promocode_issued": result.promocode is not None,
        },
    )
    await uow.commit()  # F2
    return ActivityFinishOut(
        status="ok",
        attempt_id=int(result.attempt_id),
        summary=result.summary,
        recommendations=[
            ProductCardOut(
                product_name=card.product_name,
                caption=card.caption,
                product_url=card.product_url,
            )
            for card in result.recommendations
        ],
        promocode=result.promocode,
        rewards=[
            RewardOut(
                reward_type=reward.reward_type,
                code=reward.code,
                product_recommendation_ids=(
                    list(reward.product_recommendation_ids)
                    if reward.product_recommendation_ids is not None
                    else None
                ),
            )
            for reward in result.rewards
        ],
    )


@router.post("/ugc/accept", response_model=UgcAcceptOut)
async def accept_ugc(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> UgcAcceptOut:
    payload = _parse(UgcAcceptIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    # Ссылки и tg_file_id в лог не пишем (правило 2): только факт приёма и внутренние id.
    accepted = await accept_from_user(
        r.consents,
        r.catalog,
        r.attempts,
        r.ugc,
        r.jobs,
        channel.user_id,
        AttemptId(payload.attempt_id),
        payload.tg_file_id,
        payload.mime_type,
        payload.file_size_bytes,
        payload.post_url,
        payload.subscription_url,
        clock.now(),
    )
    logger.info(
        "ugc_accepted",
        extra={
            "event": "ugc_accepted",
            "attempt_id": int(accepted.attempt_id),
            "submission_id": int(accepted.submission_id),
        },
    )
    await uow.commit()  # F2
    return UgcAcceptOut(status="ok", submission_id=int(accepted.submission_id))


@router.post("/ugc/resubmit", response_model=UgcResubmitOut)
async def resubmit_ugc(
    body: Annotated[bytes, Depends(verify_internal_hmac)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[ClockPort, Depends(get_clock)],
) -> UgcResubmitOut | JSONResponse:
    """Повтор после отказа (N2, N4). Лимит исчерпан - 409 resubmit_limit, прохождение rejected.

    409 - результат use-case, а не исключение: закрытие прохождения фиксируется при commit UoW
    (он коммитит при нормальном выходе из обработчика).
    """
    payload = _parse(UgcResubmitIn, body)
    r = uow.repositories
    channel = await lookup_user(r.channels, payload.channel_type, payload.external_id)
    if channel is None:
        raise NotFound("channel_not_found")
    result = await resubmit(
        r.consents,
        r.catalog,
        r.attempts,
        r.ugc,
        r.jobs,
        channel.user_id,
        AttemptId(payload.attempt_id),
        payload.tg_file_id,
        payload.mime_type,
        payload.file_size_bytes,
        payload.post_url,
        payload.subscription_url,
        clock.now(),
    )
    if result.kind == "limit_exceeded":
        logger.info(
            "ugc_resubmit_limit",
            extra={
                "event": "ugc_resubmit_limit",
                "attempt_id": payload.attempt_id,
                "attempt_closed": result.attempt_closed,
            },
        )
        await uow.commit()  # F2: 409 уходит только после фиксации закрытия прохождения
        return JSONResponse(
            status_code=409,
            content={"error": "resubmit_limit", "attempt_closed": result.attempt_closed},
        )
    if result.submission_id is None:
        raise RuntimeError("resubmit_success_without_submission")
    logger.info(
        "ugc_resubmitted",
        extra={
            "event": "ugc_resubmitted",
            "attempt_id": payload.attempt_id,
            "submission_id": int(result.submission_id),
        },
    )
    await uow.commit()  # F2
    return UgcResubmitOut(status="ok", submission_id=int(result.submission_id))
