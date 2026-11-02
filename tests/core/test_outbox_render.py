"""Рендер outbox в core.jobs (п.8 ревью): template + context -> готовый текст при claim.

Тексты - из конфига (правило 5); здесь они передаются явно, как их передаёт Settings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.activities.wheel import spin
from app.core.jobs import enqueue
from app.core.jobs.outbox import OutboxTexts, claim_outbox
from app.core.types.ids import ChannelAccountId, UserId
from tests.factories import create_guest
from tests.factories.brand_d import CONFIRMATION_LIMITS, WHEEL_LIMITS, make_activity, make_prize
from tests.factories.random import SeededRandomPort
from tests.factories.ugc import create_admin
from tests.factories.workers import (
    SLUG,
    ingest_done,
    reject_submission,
    brand_b_submission,
)

TEXTS = OutboxTexts(
    reward_notice="Готово! Промокод: {promocode}. Напиток: {voucher_title}. "
    "Код ваучера: {voucher_code}",
    ugc_rejected_retry="Ваша работа отклонена: {reason}. Пришлите другое фото через /start.",
    ugc_rejected_closed="Ваша работа отклонена: {reason}. К сожалению, попытки исчерпаны.",
    ingest_failed_retry="Ваш файл не удалось принять: {reason}. Попробуйте ещё раз через /start.",
    ingest_failed_closed="Технически приёмка завершилась неудачно, обратитесь к администратору.",
)


async def _claim(uow_factory: Any) -> Any:
    async with uow_factory() as uow:
        r = uow.repositories
        return await claim_outbox(
            r.jobs,
            r.rewards,
            r.catalog,
            TEXTS,
            worker_id="render-test",
            limit=10,
            visibility_timeout_s=90,
        )


async def test_ugc_rejected_text_has_both_variants(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    case = await brand_b_submission(uow_factory, sync_engine, 9101)
    await ingest_done(uow_factory, case.submission_id)
    await reject_submission(uow_factory, case.submission_id, create_admin(sync_engine))
    async with uow_factory() as uow:
        for closed in (False, True):
            await enqueue(
                uow.repositories.jobs,
                "telegram_send",
                {
                    "chat_id": int(case.guest.external_id),
                    "template": "ugc_rejected",
                    "context": {
                        "reason_slug": "low_quality",
                        "attempt_closed": closed,
                        "activity_slug": SLUG,
                    },
                },
                idempotency_key=f"render-rej:{closed}",
            )

    messages = await _claim(uow_factory)

    texts = sorted(message.text for message in messages)
    assert len(texts) == 2
    assert any("Низкое качество фото" in t and "Пришлите другое фото" in t for t in texts)
    assert any("Низкое качество фото" in t and "попытки исчерпаны" in t for t in texts)


async def test_missing_reward_is_terminal_render_failure(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    async with uow_factory() as uow:
        await enqueue(
            uow.repositories.jobs,
            "telegram_send",
            {"chat_id": 1, "template": "reward_notice", "context": {"reward_ids": [999999]}},
            idempotency_key="render-missing",
        )

    messages = await _claim(uow_factory)

    assert messages == []
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, last_error FROM jobs WHERE idempotency_key = 'render-missing'")
        ).one()
    assert row.status == "done"
    assert row.last_error == "terminal_business_error:render_failed"


async def test_unknown_template_is_terminal_render_failure(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    async with uow_factory() as uow:
        await enqueue(
            uow.repositories.jobs,
            "telegram_send",
            {"chat_id": 1, "template": "no_such_template", "context": {}},
            idempotency_key="render-unknown",
        )

    assert await _claim(uow_factory) == []
    with sync_engine.connect() as conn:
        status = conn.execute(
            text("SELECT last_error FROM jobs WHERE idempotency_key = 'render-unknown'")
        ).scalar_one()
    assert status == "terminal_business_error:render_failed"


async def test_claim_outbox_renders_system_reason_text(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """ТОП-1: ingest_failed берёт подпись из system_reject_reasons, а не из reject_reasons.

    attempt_closed=false - «Попробуйте ещё раз через /start»; true - обращение к администратору.
    Текст не содержит «Ваша работа отклонена»: это техническая неудача, а не отказ модератора.
    """
    case = await brand_b_submission(uow_factory, sync_engine, 9102)
    await ingest_done(uow_factory, case.submission_id)
    async with uow_factory() as uow:
        for closed in (False, True):
            await enqueue(
                uow.repositories.jobs,
                "telegram_send",
                {
                    "chat_id": int(case.guest.external_id),
                    "template": "ingest_failed",
                    "context": {
                        "reason_slug": "mime_mismatch",
                        "attempt_closed": closed,
                        "activity_slug": SLUG,
                    },
                },
                idempotency_key=f"render-sys:{closed}",
            )

    messages = await _claim(uow_factory)

    texts = sorted(message.text for message in messages)
    assert len(texts) == 2
    retry = next(t for t in texts if "Попробуйте ещё раз" in t)
    assert "Формат файла не поддерживается" in retry
    assert "Ваша работа отклонена" not in retry
    closed_text = next(t for t in texts if "обратитесь к администратору" in t)
    assert "Ваша работа отклонена" not in closed_text


NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
BRAND_D_TEXTS = OutboxTexts(
    reward_notice=TEXTS.reward_notice,
    ugc_rejected_retry=TEXTS.ugc_rejected_retry,
    ugc_rejected_closed=TEXTS.ugc_rejected_closed,
    ingest_failed_retry=TEXTS.ingest_failed_retry,
    ingest_failed_closed=TEXTS.ingest_failed_closed,
    wheel_result="Колесо момента: вам выпало «{prize}».",
    polaroid_ready="Ваш Polaroid готов!",
    promocode="{title}: ваш промокод {code}",
    confirmation_rejected="Подарок за пост не выдан: {reason}.",
    public_base_url="https://app.test",
)


async def _enqueue_telegram(uow_factory: Any, template: str, context: dict[str, Any]) -> None:
    async with uow_factory() as uow:
        await enqueue(
            uow.repositories.jobs,
            "telegram_send",
            {"chat_id": 5001, "template": template, "context": context},
        )


async def _claim_brand_d(uow_factory: Any, frame_link: Any = None) -> list[Any]:
    async with uow_factory() as uow:
        r = uow.repositories
        return await claim_outbox(
            r.jobs,
            r.rewards,
            r.catalog,
            BRAND_D_TEXTS,
            worker_id="render-brand-d",
            limit=10,
            visibility_timeout_s=90,
            frame_link=frame_link,
        )


async def _spin_for(uow_factory: Any, guest: Any, activity: Any, seed: int) -> Any:
    async with uow_factory() as uow:
        repos = uow.repositories
        return await spin(
            repos.consents,
            repos.catalog,
            repos.attempts,
            repos.jobs,
            repos.rewards,
            repos.wheel,
            SeededRandomPort(seed),
            UserId(guest.user_id),
            activity.slug,
            ChannelAccountId(guest.channel_account_id),
            NOW,
        )


async def test_render_wheel_result(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """M-3: wheel_result - текст с названием приза из награды колеса."""
    guest = create_guest(sync_engine, external_id="3701", now=NOW)
    activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    make_prize(sync_engine, activity.activity_id, "Комплимент стилиста", "compliment", 1)
    result = await _spin_for(uow_factory, guest, activity, 1)
    await _enqueue_telegram(uow_factory, "wheel_result", {"reward_ids": [result.reward_id]})

    messages = await _claim_brand_d(uow_factory)

    assert len(messages) == 1
    assert messages[0].text == "Колесо момента: вам выпало «Комплимент стилиста»."
    assert messages[0].media_url is None


async def test_render_promocode(uow_factory: Any, clean_db: None, sync_engine: Engine) -> None:
    """M-3: promocode - текст с кодом награды (код генерируется по шаблону приза)."""
    guest = create_guest(sync_engine, external_id="3702", now=NOW)
    activity = make_activity(sync_engine, "wheel", WHEEL_LIMITS)
    make_prize(
        sync_engine,
        activity.activity_id,
        "Промокод -10%",
        "promocode",
        1,
        code_template="BRAND_D10-{random8}",
    )
    result = await _spin_for(uow_factory, guest, activity, 2)
    await _enqueue_telegram(uow_factory, "promocode", {"reward_ids": [result.reward_id]})

    messages = await _claim_brand_d(uow_factory)

    assert len(messages) == 1
    assert result.code is not None and result.code.startswith("BRAND_D10-")
    assert messages[0].text == f"Промокод -10%: ваш промокод {result.code}"


async def test_render_polaroid_ready(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """M-3: polaroid_ready - подпись без ссылки и media_url = PUBLIC_BASE_URL + относительная
    ссылка."""
    await _enqueue_telegram(uow_factory, "polaroid_ready", {"frame_submission_id": 77})

    messages = await _claim_brand_d(uow_factory, lambda fid: f"/media/frame/{fid}?t=signed")

    assert len(messages) == 1
    assert messages[0].text == "Ваш Polaroid готов!"
    assert messages[0].media_url == "https://app.test/media/frame/77?t=signed"


async def test_render_confirmation_rejected(
    uow_factory: Any, clean_db: None, sync_engine: Engine
) -> None:
    """M-3: confirmation_rejected - подпись причины из activities.limits.reject_reasons."""
    activity = make_activity(sync_engine, "confirmation", CONFIRMATION_LIMITS)
    await _enqueue_telegram(
        uow_factory,
        "confirmation_rejected",
        {"reason_slug": "not_posted", "activity_slug": activity.slug, "attempt_closed": True},
    )

    messages = await _claim_brand_d(uow_factory)

    assert len(messages) == 1
    assert messages[0].text == "Подарок за пост не выдан: Пост не опубликован."
