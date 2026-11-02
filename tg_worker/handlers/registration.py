from __future__ import annotations

import logging

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from tg_worker.config import get_worker_settings
from tg_worker.handlers.main_menu import show_main_menu
from tg_worker.handlers.states import RegistrationStates
from tg_worker.internal_client import InternalApiClient

logger = logging.getLogger("tg_worker.handlers.registration")

# Константа согласуется с app/core/consent/policy.py::CURRENT_POLICY_VERSION.
# Правило 5: конкретное значение дублируется, но tg_worker — тонкий адаптер,
# он не ходит в ядро только за константой; при расхождении /pii/update
# вернёт 422 policy_version_unsupported.
POLICY_VERSION_SENT: str = "v1"

router = Router()


def _client() -> InternalApiClient:
    settings = get_worker_settings()
    hmac_key = settings.internal_api_auth_secret
    return InternalApiClient(
        base_url=settings.internal_api_url, auth_secret=hmac_key
    )


@router.message(RegistrationStates.WaitFirstName)
async def wait_first_name(message: Message, state: FSMContext) -> None:
    first_name = (message.text or "").strip()
    if not first_name:
        await message.answer("Имя не может быть пустым. Введите ваше имя:")
        return
    await state.update_data(first_name=first_name)
    await state.set_state(RegistrationStates.WaitLastName)
    await message.answer("Фамилия?")


@router.message(RegistrationStates.WaitLastName)
async def wait_last_name(message: Message, state: FSMContext) -> None:
    last_name = (message.text or "").strip()
    if not last_name:
        await message.answer("Фамилия не может быть пустой. Введите фамилию:")
        return
    await state.update_data(last_name=last_name)
    await state.set_state(RegistrationStates.WaitPhone)
    await message.answer("Телефон в формате +7...?")


@router.message(RegistrationStates.WaitPhone)
async def wait_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not phone:
        await message.answer("Телефон не может быть пустым. Введите телефон:")
        return
    await state.update_data(phone=phone)
    await state.set_state(RegistrationStates.WaitEmail)
    await message.answer("Email?")


@router.message(RegistrationStates.WaitEmail)
async def wait_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if not email:
        await message.answer("Email не может быть пустым. Введите email:")
        return

    data = await state.get_data()
    external_id = str(data.get("external_id") or "")
    first_name = str(data.get("first_name") or "")
    last_name = str(data.get("last_name") or "")
    phone = str(data.get("phone") or "")
    client = _client()
    status, body = await client.update_pii(
        channel_type="telegram",
        external_id=external_id,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        email=email,
        policy_version=POLICY_VERSION_SENT,
    )
    if status == 422:
        reason = body.get("error") if isinstance(body, dict) else None
        if reason == "phone_invalid":
            await state.set_state(RegistrationStates.WaitPhone)
            await message.answer(
                "Телефон в неверном формате. Пример: +79001234567. Попробуйте ещё раз:"
            )
            return
        if reason == "email_invalid":
            await message.answer(
                "Email в неверном формате. Пример: name@example.com. Попробуйте ещё раз:"
            )
            return
        await message.answer("Поле заполнено неверно, попробуйте ещё раз:")
        return
    if status == 409:
        reason = body.get("error") if isinstance(body, dict) else None
        if reason == "phone_taken":
            await state.set_state(RegistrationStates.WaitPhone)
            await message.answer(
                "Этот телефон уже зарегистрирован. Укажите другой номер в формате +7...:"
            )
            return
        if reason == "email_taken":
            await message.answer(
                "Этот email уже зарегистрирован. Укажите другой email:"
            )
            return
        await state.clear()
        await message.answer(
            "Согласие на обработку данных не найдено — начните заново через /start."
        )
        return
    if status != 200:
        logger.error(
            "pii_update_failed",
            extra={"event": "pii_update_failed", "status": status},
        )
        await message.answer("Внутренняя ошибка, попробуйте позже.")
        return

    await state.clear()
    await message.answer("Готово! Выберите бренд:")
    await show_main_menu(message)
