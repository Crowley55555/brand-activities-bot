from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from tg_worker.config import get_worker_settings
from tg_worker.fsm_storage import HttpFsmStorage
from tg_worker.handlers import router
from tg_worker.internal_client.httpx_json import InternalApiClient
from tg_worker.outbox_puller.puller import default_worker_id, run_outbox_puller

logger = logging.getLogger("tg_worker.bot")


async def run_polling() -> None:
    settings = get_worker_settings()
    tg_cred = settings.telegram_bot_token
    if not tg_cred:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    bot = Bot(token=tg_cred)
    # ADR-32 (фаза 3c): outbox puller шлёт фоновые уведомления рядом с polling.
    # worker_id совпадает в claim и ack: по нему ядро проверяет замок (fencing, ТОП-3).
    api = InternalApiClient(settings.internal_api_url, settings.internal_api_auth_secret)
    # C-4 (фаза 10a): FSM-storage через HTTP под HMAC. MemoryStorage терял состояние
    # регистрации при любом рестарте tg_worker; теперь второй экземпляр подхватит те же
    # данные из ядра (правило 4 — tg_worker в БД не ходит).
    dp = Dispatcher(storage=HttpFsmStorage(api))
    dp.include_router(router)
    worker_id = settings.outbox_worker_id or default_worker_id()
    stop = asyncio.Event()
    puller = asyncio.create_task(run_outbox_puller(bot, api, worker_id, stop))
    logger.info("tg_worker starting polling")
    try:
        await dp.start_polling(bot)
    finally:
        stop.set()
        await puller
        await api.aclose()
        await bot.session.close()
