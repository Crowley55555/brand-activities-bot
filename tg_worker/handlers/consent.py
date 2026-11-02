from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from tg_worker.config import get_worker_settings
from tg_worker.handlers.states import ConsentStates, RegistrationStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.consent")

router = Router()


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(
        base_url=settings.internal_api_url, auth_secret=hmac_key
    )


@router.callback_query(
    ConsentStates.WaitConsent, F.data == "consent:accept"
)
async def accept_consent_callback(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    external_id = data.get("external_id") or (
        str(cb.from_user.id) if cb.from_user else "0"
    )
    client = _client()
    try:
        await client.accept_consent("telegram", str(external_id))
    except Exception:
        logger.exception("consent_accept_failed")
        await cb.answer("Не получилось принять политику. Попробуйте позже.")
        return

    await state.set_state(RegistrationStates.WaitFirstName)
    await state.update_data(external_id=str(external_id))
    await cb.answer()
    if cb.message is not None:
        await cb.message.answer("Как вас зовут? (имя)")
