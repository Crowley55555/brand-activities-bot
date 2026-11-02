"""tg UGC-handler (фаза 6.5, known-limit B-1): фото и, если активность просит, две ссылки.

Апдейты идут через корневой роутер tg_worker с настоящими фильтрами состояний и FSM
(MemoryStorage): так проверяются и порядок шагов, и то, что роутер подключён. Бот работает на
фейковой сессии (сети нет), ядро подменено `_Core`: ответы задаёт тест, вызовы записываются.
Флаги `requires_*` приходят в словаре активности (`activity["limits"]`), slug'и тут условные.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Message
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.activities.ugc.service import ACTIVITY_KIND
from tests.conftest import TEST_HMAC
from tests.factories.ugc import attempt_status, job_rows, ugc_rows
from tg_worker.handlers import main_menu, ugc
from tg_worker.handlers import router as root_router
from tg_worker.handlers.states import UgcStates
from tg_worker.internal_client import InternalApiClient

CHAT_ID = 4242
SLUG = "ugc_demo"
ATTEMPT_ID = 77
POST_URL = "https://example.com/post/1"
SUBSCRIPTION_URL = "https://example.com/profile"
CONFLICT_TEXT = "уже участвовали"

_message_ids = itertools.count(1)


class _Session(BaseSession):
    """Сессия бота без сети: запоминает вызовы, на отправку отвечает готовым Message."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Any] = []

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message.model_validate(
                _raw_message(text=method.text), context={"bot": bot}
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        yield b""


class _Core:
    """Подмена InternalApiClient: сигнатуры те же, что у настоящего клиента."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.accept_calls: list[dict[str, Any]] = []
        self.brand_activities: list[dict[str, Any]] = []
        self.start_result: tuple[int, dict[str, Any]] = (200, _start_ok())
        self.accept_result: tuple[int, dict[str, Any]] = (200, {"status": "ok"})
        self.start_exception: Exception | None = None
        self.accept_exception: Exception | None = None

    async def list_brand_activities(self, brand_slug: str) -> list[dict[str, Any]]:
        return self.brand_activities

    async def start_activity(
        self, channel_type: str, external_id: str, activity_slug: str
    ) -> tuple[int, dict[str, Any]]:
        self.start_calls.append(
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "activity_slug": activity_slug,
            }
        )
        if self.start_exception is not None:
            raise self.start_exception
        return self.start_result

    async def ugc_accept(
        self,
        channel_type: str,
        external_id: str,
        attempt_id: int,
        tg_file_id: str,
        mime_type: str,
        file_size_bytes: int,
        post_url: str | None = None,
        subscription_url: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.accept_calls.append(
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "attempt_id": attempt_id,
                "tg_file_id": tg_file_id,
                "mime_type": mime_type,
                "file_size_bytes": file_size_bytes,
                "post_url": post_url,
                "subscription_url": subscription_url,
            }
        )
        if self.accept_exception is not None:
            raise self.accept_exception
        return self.accept_result


def _start_ok(slug: str = SLUG) -> dict[str, Any]:
    return {
        "status": "ok",
        "attempt_id": ATTEMPT_ID,
        "activity_slug": slug,
        "kind": "ugc",
        "total_questions": 0,
        "question": None,
        "next_step": {"kind": "ask_photo"},
    }


def _activity(*, post: bool, subscription: bool, slug: str = SLUG) -> dict[str, Any]:
    return {
        "slug": slug,
        "display_name": "Фото",
        "kind": "ugc",
        "limits": {"requires_post_url": post, "requires_subscription_url": subscription},
    }


def _raw_message(
    *,
    text: str | None = None,
    photo: list[dict[str, Any]] | None = None,
    media_group_id: str | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "message_id": next(_message_ids),
        "date": 1760212800,
        "chat": {"id": CHAT_ID, "type": "private"},
        "from": {"id": CHAT_ID, "is_bot": False, "first_name": "T"},
    }
    if text is not None:
        raw["text"] = text
    if photo is not None:
        raw["photo"] = photo
    if media_group_id is not None:
        raw["media_group_id"] = media_group_id
    return raw


def _sizes(*, with_size: bool = True, largest: int = 30_000) -> list[dict[str, Any]]:
    """Три варианта фото по возрастанию, последний - самый большой (так отдаёт Telegram)."""
    sizes: list[dict[str, Any]] = []
    for index, size in enumerate((1_000, 9_000, largest), start=1):
        item: dict[str, Any] = {
            "file_id": f"F{index}",
            "file_unique_id": f"U{index}",
            "width": 90 * index,
            "height": 90 * index,
        }
        if with_size:
            item["file_size"] = size
        sizes.append(item)
    return sizes


@pytest.fixture
def session() -> _Session:
    return _Session()


@pytest.fixture
def bot(session: _Session) -> Bot:
    return Bot("42:TEST", session=session)


@pytest.fixture
def state(bot: Bot) -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=CHAT_ID))


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> _Core:
    fake = _Core()
    monkeypatch.setattr(ugc, "_client", lambda: fake)
    monkeypatch.setattr(main_menu, "_client", lambda: fake)
    return fake


def _texts(session: _Session) -> list[str]:
    return [str(m.text) for m in session.sent if isinstance(m, SendMessage)]


def _callback(bot: Bot, data: str) -> CallbackQuery:
    raw = {
        "id": "cb1",
        "from": {"id": CHAT_ID, "is_bot": False, "first_name": "T"},
        "chat_instance": "ci1",
        "data": data,
        "message": _raw_message(text="menu"),
    }
    return CallbackQuery.model_validate(raw, context={"bot": bot})


async def _feed_message(bot: Bot, state: FSMContext, raw: dict[str, Any]) -> Any:
    message = Message.model_validate(raw, context={"bot": bot})
    return await root_router.propagate_event(
        "message", message, state=state, raw_state=await state.get_state(), bot=bot
    )


async def _send_text(bot: Bot, state: FSMContext, text: str) -> None:
    await _feed_message(bot, state, _raw_message(text=text))


async def _send_photo(
    bot: Bot,
    state: FSMContext,
    *,
    with_size: bool = True,
    media_group_id: str | None = None,
    largest: int = 30_000,
) -> None:
    raw = _raw_message(
        photo=_sizes(with_size=with_size, largest=largest), media_group_id=media_group_id
    )
    await _feed_message(bot, state, raw)


async def _feed_callback(bot: Bot, state: FSMContext, data: str) -> None:
    await root_router.propagate_event(
        "callback_query",
        _callback(bot, data),
        state=state,
        raw_state=await state.get_state(),
        bot=bot,
    )


async def _launch(bot: Bot, state: FSMContext, *, post: bool, subscription: bool) -> None:
    await ugc.launch_ugc(
        _callback(bot, "activity:x"), state, _activity(post=post, subscription=subscription)
    )


# --- T1: старт ---------------------------------------------------------------------------------


async def test_launch_starts_activity_and_waits_for_photo(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=True, subscription=False)

    assert core.start_calls == [
        {"channel_type": "telegram", "external_id": str(CHAT_ID), "activity_slug": SLUG}
    ]
    assert await state.get_state() == UgcStates.WaitPhoto.state
    assert await state.get_data() == {
        "activity_slug": SLUG,
        "attempt_id": ATTEMPT_ID,
        "requires_post_url": True,
        "requires_subscription_url": False,
        "post_url": None,
        "subscription_url": None,
        "tg_file_id": None,
        "mime_type": None,
        "file_size_bytes": None,
    }
    assert _texts(session) == ["Пришлите фото."]


# --- T2: активность без ссылок -----------------------------------------------------------------


async def test_photo_only_activity_is_accepted_right_after_photo(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=False, subscription=False)

    await _send_photo(bot, state)

    assert core.accept_calls == [
        {
            "channel_type": "telegram",
            "external_id": str(CHAT_ID),
            "attempt_id": ATTEMPT_ID,
            "tg_file_id": "F3",  # самый большой вариант фото
            "mime_type": "image/jpeg",
            "file_size_bytes": 30_000,
            "post_url": None,
            "subscription_url": None,
        }
    ]
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert "Фото принято" in _texts(session)[-1]


# --- T3: активность со ссылками ----------------------------------------------------------------


async def test_activity_with_both_links_walks_through_all_steps(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=True, subscription=True)

    await _send_photo(bot, state)
    assert await state.get_state() == UgcStates.WaitPostUrl.state
    assert core.accept_calls == []
    assert "ссылку на пост" in _texts(session)[-1]

    await _send_text(bot, state, POST_URL)
    assert await state.get_state() == UgcStates.WaitSubscriptionUrl.state
    assert core.accept_calls == []
    assert "профиль подписки" in _texts(session)[-1]

    await _send_text(bot, state, SUBSCRIPTION_URL)
    assert core.accept_calls == [
        {
            "channel_type": "telegram",
            "external_id": str(CHAT_ID),
            "attempt_id": ATTEMPT_ID,
            "tg_file_id": "F3",
            "mime_type": "image/jpeg",
            "file_size_bytes": 30_000,
            "post_url": POST_URL,
            "subscription_url": SUBSCRIPTION_URL,
        }
    ]
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert "Фото принято" in _texts(session)[-1]


@pytest.mark.parametrize(
    ("post", "subscription", "link_steps"),
    [
        (True, False, [UgcStates.WaitPostUrl]),
        (False, True, [UgcStates.WaitSubscriptionUrl]),
    ],
)
async def test_only_requested_link_is_asked(
    bot: Bot,
    state: FSMContext,
    core: _Core,
    post: bool,
    subscription: bool,
    link_steps: list[Any],
) -> None:
    """Одна ссылка из двух: второй шаг пропускается, в ядро уходит только запрошенная."""
    await _launch(bot, state, post=post, subscription=subscription)

    await _send_photo(bot, state)
    assert await state.get_state() == link_steps[0].state

    await _send_text(bot, state, POST_URL)

    assert await state.get_state() is None
    [call] = core.accept_calls
    assert call["post_url"] == (POST_URL if post else None)
    assert call["subscription_url"] == (POST_URL if subscription else None)


# --- T4: невалидные ссылки ---------------------------------------------------------------------

BAD_URLS = [
    "http://example.com/post",  # ядро принимает только https
    "example.com/post",
    "https://",
    "https://a b",
    "javascript:alert(1)",
    "https://" + "a" * 2041,
    "вот моя ссылка: https://example.com/post",
]


@pytest.mark.parametrize("bad_url", BAD_URLS)
async def test_invalid_post_url_keeps_state_and_never_reaches_core(
    bot: Bot, state: FSMContext, session: _Session, core: _Core, bad_url: str
) -> None:
    await _launch(bot, state, post=True, subscription=True)
    await _send_photo(bot, state)
    sent_before = len(_texts(session))

    await _send_text(bot, state, bad_url)

    assert await state.get_state() == UgcStates.WaitPostUrl.state
    assert (await state.get_data())["post_url"] is None
    assert core.accept_calls == []
    [hint] = _texts(session)[sent_before:]
    assert "https://" in hint


async def test_invalid_subscription_url_keeps_state(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=True, subscription=True)
    await _send_photo(bot, state)
    await _send_text(bot, state, POST_URL)

    await _send_text(bot, state, "не ссылка")

    assert await state.get_state() == UgcStates.WaitSubscriptionUrl.state
    assert (await state.get_data())["post_url"] == POST_URL
    assert (await state.get_data())["subscription_url"] is None
    assert core.accept_calls == []
    assert "https://" in _texts(session)[-1]


async def test_scheme_case_and_spaces_around_url_are_normalized(
    bot: Bot, state: FSMContext, core: _Core
) -> None:
    """Мобильная клавиатура пишет «Https://» с заглавной: ядру уходит нормальная ссылка."""
    await _launch(bot, state, post=True, subscription=False)
    await _send_photo(bot, state)

    await _send_text(bot, state, "  Https://example.com/post/1 \n")

    [call] = core.accept_calls
    assert call["post_url"] == "https://example.com/post/1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/p", "https://example.com/p"),
        ("  https://example.com/p\n", "https://example.com/p"),
        ("HTTPS://Example.com/P", "https://Example.com/P"),
        ("https://abc", "https://abc"),
        ("https://" + "a" * 2040, "https://" + "a" * 2040),
        ("http://example.com/p", None),
        ("ftp://example.com/p", None),
        ("https://", None),
        ("https://ab", None),
        ("https://a b", None),
        ("https://" + "a" * 2041, None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_url_matches_core_boundary(raw: str | None, expected: str | None) -> None:
    """Граница та же, что у UgcAcceptIn в ядре: https, без пробелов, не длиннее 2048 символов."""
    assert ugc.normalize_url(raw) == expected


# --- непредусмотренный ввод --------------------------------------------------------------------


async def test_text_instead_of_photo_gets_hint_and_keeps_state(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=False, subscription=False)

    await _send_text(bot, state, "вот моё фото")

    assert await state.get_state() == UgcStates.WaitPhoto.state
    assert core.accept_calls == []
    assert "фото" in _texts(session)[-1]
    assert len(_texts(session)) == 2  # запрос фото и подсказка


async def test_photo_instead_of_link_gets_hint(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=True, subscription=False)
    await _send_photo(bot, state)

    await _send_photo(bot, state)

    assert await state.get_state() == UgcStates.WaitPostUrl.state
    assert core.accept_calls == []
    assert "https://" in _texts(session)[-1]


async def test_extra_album_photos_are_ignored_quietly(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    """Гость выбрал несколько фото разом: первое обработано, остальные не засоряют чат."""
    await _launch(bot, state, post=True, subscription=False)
    await _send_photo(bot, state, media_group_id="album1")
    sent_before = len(_texts(session))

    await _send_photo(bot, state, media_group_id="album1")
    await _send_photo(bot, state, media_group_id="album1")

    assert len(_texts(session)) == sent_before
    assert await state.get_state() == UgcStates.WaitPostUrl.state


async def test_photo_without_declared_size_is_not_sent_to_core(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=False, subscription=False)

    await _send_photo(bot, state, with_size=False)

    assert core.accept_calls == []
    assert await state.get_state() == UgcStates.WaitPhoto.state
    assert "другое фото" in _texts(session)[-1]


# --- T5: ошибки старта -------------------------------------------------------------------------


async def test_start_conflict_clears_fsm_and_tells_guest(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    core.start_result = (409, {"error": "already_passed_today"})
    await state.update_data(brand_activities=[{"slug": SLUG}])

    await _launch(bot, state, post=True, subscription=True)

    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert CONFLICT_TEXT in _texts(session)[-1]
    assert core.accept_calls == []


async def test_second_click_on_started_activity_reminds_the_current_step(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    """Двойной тап по кнопке: ядро отвечает 409, но начатое прохождение не должно пропасть.

    Прохождение уже открыто (in_progress держит «раз в день» до истечения срока), поэтому
    сброс состояния оставил бы гостя без возможности прислать фото.
    """
    await _launch(bot, state, post=True, subscription=True)
    core.start_result = (409, {"error": "already_passed_today"})

    await _launch(bot, state, post=True, subscription=True)
    assert await state.get_state() == UgcStates.WaitPhoto.state
    assert (await state.get_data())["attempt_id"] == ATTEMPT_ID
    assert CONFLICT_TEXT not in _texts(session)[-1]
    assert "фото" in _texts(session)[-1]

    await _send_photo(bot, state)
    await _launch(bot, state, post=True, subscription=True)
    assert await state.get_state() == UgcStates.WaitPostUrl.state
    assert "ссылку на пост" in _texts(session)[-1]


async def test_conflict_for_another_activity_is_reported_not_resumed(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    await _launch(bot, state, post=False, subscription=False)
    core.start_result = (409, {"error": "already_passed_today"})

    await ugc.launch_ugc(
        _callback(bot, "activity:y"), state, _activity(post=False, subscription=False, slug="other")
    )

    assert await state.get_state() is None
    assert CONFLICT_TEXT in _texts(session)[-1]


async def test_start_without_consent_points_to_start_command(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    core.start_result = (409, {"error": "consent_required"})

    await _launch(bot, state, post=False, subscription=False)

    assert await state.get_state() is None
    assert "/start" in _texts(session)[-1]


async def test_start_failure_is_reported_without_leaving_a_state(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    core.start_exception = httpx.ConnectError("core is down")

    await _launch(bot, state, post=False, subscription=False)

    assert await state.get_state() is None
    assert "Попробуйте позже" in _texts(session)[-1]


async def test_start_answer_without_attempt_id_is_an_error(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    core.start_result = (200, {"status": "ok"})

    await _launch(bot, state, post=False, subscription=False)

    assert await state.get_state() is None
    assert "Попробуйте позже" in _texts(session)[-1]


async def test_activity_without_limits_is_not_started(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    """Ядро не передало limits - что просить у гостя, неизвестно: прохождение не открываем."""
    activity = {"slug": SLUG, "display_name": "Фото", "kind": "ugc"}

    await ugc.launch_ugc(_callback(bot, "activity:x"), state, activity)

    assert core.start_calls == []
    assert await state.get_state() is None
    assert _texts(session) == ["Эта активность пока недоступна в боте."]


@pytest.mark.parametrize("bad_value", ["true", "yes", 1, 0, None, ["post"]])
async def test_non_boolean_flag_is_a_configuration_error_not_a_silent_skip(
    bot: Bot, state: FSMContext, session: _Session, core: _Core, bad_value: Any
) -> None:
    """Флаг не boolean: молчаливое «ссылка не нужна» оставило бы модератора без ссылок."""
    activity = _activity(post=True, subscription=False)
    activity["limits"]["requires_post_url"] = bad_value

    await ugc.launch_ugc(_callback(bot, "activity:x"), state, activity)

    assert core.start_calls == []
    assert await state.get_state() is None
    assert _texts(session) == ["Эта активность пока недоступна в боте."]


async def test_missing_flags_mean_photo_only(
    bot: Bot, state: FSMContext, core: _Core
) -> None:
    """В limits есть другие ключи, а флагов нет: ссылки не просим (активность без ссылок)."""
    activity = {"slug": SLUG, "kind": "ugc", "limits": {"max_per_day": 1}}

    await ugc.launch_ugc(_callback(bot, "activity:x"), state, activity)
    await _send_photo(bot, state)

    [call] = core.accept_calls
    assert call["post_url"] is None
    assert call["subscription_url"] is None
    assert await state.get_state() is None


# --- ошибки приёмки ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "text_part"),
    [
        (422, "file_too_large", "слишком большое"),
        (422, "mime_not_allowed", "другое фото"),
        (422, "file_size_invalid", "другое фото"),
        (422, "bad_request", "ещё раз"),
    ],
)
async def test_fixable_accept_error_returns_guest_to_photo_step(
    bot: Bot, state: FSMContext, session: _Session, core: _Core, status: int, error: str,
    text_part: str,
) -> None:
    """Прохождение осталось открытым: сброс FSM лишил бы гостя шанса (повторный старт даст 409)."""
    await _launch(bot, state, post=True, subscription=True)
    await _send_photo(bot, state)
    await _send_text(bot, state, POST_URL)
    core.accept_result = (status, {"error": error})

    await _send_text(bot, state, SUBSCRIPTION_URL)

    assert await state.get_state() == UgcStates.WaitPhoto.state
    data = await state.get_data()
    assert data["attempt_id"] == ATTEMPT_ID
    assert data["post_url"] is None
    assert data["subscription_url"] is None
    assert data["tg_file_id"] is None
    assert text_part in _texts(session)[-1]

    # Новое фото проходит весь путь заново и принимается.
    core.accept_result = (200, {"status": "ok"})
    await _send_photo(bot, state)
    await _send_text(bot, state, POST_URL)
    await _send_text(bot, state, SUBSCRIPTION_URL)
    assert await state.get_state() is None
    assert len(core.accept_calls) == 2
    assert "Фото принято" in _texts(session)[-1]


@pytest.mark.parametrize(
    ("status", "error", "text_part"),
    [
        (409, "consent_required", "/start"),
        (409, "attempt_not_in_progress", "Откройте активность"),
        (404, "attempt_not_found", "Откройте активность"),
        (403, "attempt_not_owned", "Откройте активность"),
        (404, "channel_not_found", "/start"),
        (422, "activity_kind_mismatch", "Попробуйте позже"),
    ],
)
async def test_final_accept_error_clears_fsm(
    bot: Bot, state: FSMContext, session: _Session, core: _Core, status: int, error: str,
    text_part: str,
) -> None:
    await _launch(bot, state, post=False, subscription=False)
    core.accept_result = (status, {"error": error})

    await _send_photo(bot, state)

    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert text_part in _texts(session)[-1]
    assert error not in _texts(session)[-1]


@pytest.mark.parametrize("failure", ["server_error", "network_down"])
async def test_transient_accept_failure_returns_guest_to_photo_step(
    bot: Bot, state: FSMContext, session: _Session, core: _Core, failure: str
) -> None:
    await _launch(bot, state, post=False, subscription=False)
    if failure == "server_error":
        core.accept_result = (500, {})
    else:
        core.accept_exception = httpx.ConnectError("core is down")

    await _send_photo(bot, state)

    assert await state.get_state() == UgcStates.WaitPhoto.state
    assert (await state.get_data())["attempt_id"] == ATTEMPT_ID
    assert "ещё раз" in _texts(session)[-1]


def test_error_texts_map_known_codes_and_hide_unknown() -> None:
    assert "/start" in ugc.ugc_error_text("consent_required")
    assert CONFLICT_TEXT in ugc.ugc_error_text("already_passed_today")
    assert "слишком большое" in ugc.ugc_error_text("file_too_large")
    fallback = ugc.ugc_error_text("some_internal_code")
    assert "some_internal_code" not in fallback
    assert "Не получилось" in fallback


async def test_state_is_released_before_the_core_call(
    bot: Bot, state: FSMContext, core: _Core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Альбом: пока ядро принимает первое фото, состояния ожидания уже нет.

    Второе фото из альбома приходит отдельным апдейтом и не должно попасть в тот же шаг: иначе
    ядро получило бы второй accept и ответило ложным attempt_not_in_progress.
    """
    await _launch(bot, state, post=False, subscription=False)
    seen_states: list[str | None] = []
    original = core.ugc_accept

    async def spying_accept(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        seen_states.append(await state.get_state())  # состояние во время запроса к ядру
        return await original(*args, **kwargs)

    monkeypatch.setattr(core, "ugc_accept", spying_accept)

    await _send_photo(bot, state)

    assert seen_states == [None]
    assert len(core.accept_calls) == 1


# --- диспетчер меню и подключение роутера ------------------------------------------------------


async def test_brand_with_single_ugc_activity_starts_it(
    bot: Bot, state: FSMContext, core: _Core
) -> None:
    core.brand_activities = [_activity(post=True, subscription=False)]

    await _feed_callback(bot, state, "brand:demo")

    assert core.start_calls[0]["activity_slug"] == SLUG
    assert await state.get_state() == UgcStates.WaitPhoto.state


async def test_ugc_button_among_several_activities_starts_it(
    bot: Bot, state: FSMContext, core: _Core
) -> None:
    survey = {"slug": "demo_survey", "display_name": "Опрос", "kind": "survey"}
    core.brand_activities = [survey, _activity(post=False, subscription=False)]
    await _feed_callback(bot, state, "brand:demo")

    await _feed_callback(bot, state, f"activity:{SLUG}")

    assert [c["activity_slug"] for c in core.start_calls] == [SLUG]
    assert await state.get_state() == UgcStates.WaitPhoto.state


async def test_unknown_kind_keeps_unavailable_fallback(
    bot: Bot, state: FSMContext, session: _Session, core: _Core
) -> None:
    core.brand_activities = [{"slug": "future", "display_name": "Новое", "kind": "something_new"}]

    await _feed_callback(bot, state, "brand:demo")

    assert core.start_calls == []
    assert _texts(session) == ["Эта активность пока недоступна в боте."]


def test_ugc_router_is_registered_and_kind_matches_core() -> None:
    assert ugc.router in root_router.sub_routers
    assert ugc.UGC_KIND == ACTIVITY_KIND


# --- клиент ядра -------------------------------------------------------------------------------


async def test_ugc_accept_sends_signed_json_and_does_not_retry_on_server_error() -> None:
    """retry=False: повтор после 5xx дал бы ложный attempt_not_in_progress."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"error": "boom"})

    client = InternalApiClient(base_url="http://core", auth_secret="hmac-k")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    status, body = await client.ugc_accept(
        channel_type="telegram",
        external_id="4242",
        attempt_id=ATTEMPT_ID,
        tg_file_id="F3",
        mime_type="image/jpeg",
        file_size_bytes=30_000,
        post_url=POST_URL,
    )

    assert (status, body) == (500, {"error": "boom"})
    [request] = requests  # без повтора
    assert request.url.path == "/internal/v1/activities/ugc/accept"
    assert json.loads(request.content) == {
        "channel_type": "telegram",
        "external_id": "4242",
        "attempt_id": ATTEMPT_ID,
        "tg_file_id": "F3",
        "mime_type": "image/jpeg",
        "file_size_bytes": 30_000,
        "post_url": POST_URL,
        "subscription_url": None,
    }
    expected_signature = hmac.new(b"hmac-k", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Internal-Auth"] == expected_signature
    await client.aclose()


# --- настоящее ядро (ASGI + Postgres) ----------------------------------------------------------
# Handler и InternalApiClient настоящие, подменён только Telegram. Флаги requires_* берутся из БД
# так, как их записала миграция 0016: их увидел бы handler, если бы каталог отдавал limits.

WITH_LINKS_SLUG = "brand_b_photo_subscription"
PHOTO_ONLY_SLUG = "brand_e_photo"


def _db_activity(engine: Engine, slug: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT slug, display_name, kind, limits FROM activities WHERE slug = :slug"),
            {"slug": slug},
        ).one()
    return {
        "slug": row.slug,
        "display_name": row.display_name,
        "kind": row.kind,
        "limits": dict(row.limits),
    }


@pytest_asyncio.fixture
async def real_core(
    api_client: httpx.AsyncClient, clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> InternalApiClient:
    """Настоящий клиент поверх ASGI-приложения: подпись HMAC проверяет само ядро.

    Тем же клиентом меню бренда читает каталог, как в боте.
    """
    hmac_key = TEST_HMAC
    client = InternalApiClient(base_url="http://test", auth_secret=hmac_key)
    await client._client.aclose()
    client._client = api_client
    monkeypatch.setattr(ugc, "_client", lambda: client)
    monkeypatch.setattr(main_menu, "_client", lambda: client)
    return client


async def _register_guest(core: InternalApiClient, *, consent: bool = True) -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1760212800,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "username": "guest"},
            "text": "/start",
        },
    }
    await core.send_update(update, datetime(2026, 10, 12, 5, 0, tzinfo=UTC))
    if consent:
        await core.accept_consent("telegram", str(CHAT_ID))


async def _launch_from_db(
    bot: Bot, state: FSMContext, engine: Engine, db_slug: str, **overrides: Any
) -> None:
    """Запуск активности так, как её описывает БД; `overrides` подменяют поля словаря."""
    activity = {**_db_activity(engine, db_slug), **overrides}
    await ugc.launch_ugc(_callback(bot, "activity:x"), state, activity)


async def test_flags_from_migration_drive_photo_only_flow(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    await _register_guest(real_core)
    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)
    assert await state.get_state() == UgcStates.WaitPhoto.state
    attempt_id = (await state.get_data())["attempt_id"]

    await _send_photo(bot, state)

    assert await state.get_state() is None
    assert "Фото принято" in _texts(session)[-1]
    [row] = ugc_rows(sync_engine, attempt_id)
    assert (row.status, row.post_url, row.subscription_claimed_url) == ("receiving", None, None)
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"
    [job] = job_rows(sync_engine, "ingest_ugc_file")
    assert job.payload["tg_file_id"] == "F3"  # в загрузку ушло самое большое фото


async def test_flags_from_migration_drive_links_flow(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    await _register_guest(real_core)
    await _launch_from_db(bot, state, sync_engine, WITH_LINKS_SLUG)
    attempt_id = (await state.get_data())["attempt_id"]

    await _send_photo(bot, state)
    assert await state.get_state() == UgcStates.WaitPostUrl.state
    await _send_text(bot, state, POST_URL)
    assert await state.get_state() == UgcStates.WaitSubscriptionUrl.state
    assert ugc_rows(sync_engine, attempt_id) == []  # пока гость не прислал всё, ядро не вызвано
    await _send_text(bot, state, SUBSCRIPTION_URL)

    assert await state.get_state() is None
    assert "Фото принято" in _texts(session)[-1]
    [row] = ugc_rows(sync_engine, attempt_id)
    assert row.post_url == POST_URL
    assert row.subscription_claimed_url == SUBSCRIPTION_URL


async def test_brand_click_with_links_goes_through_real_catalog(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    """Контракт каталога и handler'а (ADR-45): флаги приходят в словаре активности от ядра."""
    await _register_guest(real_core)

    await _feed_callback(bot, state, "brand:brand_b")  # одна активность: запуск сразу
    assert await state.get_state() == UgcStates.WaitPhoto.state
    attempt_id = (await state.get_data())["attempt_id"]
    await _send_photo(bot, state)
    await _send_text(bot, state, POST_URL)
    await _send_text(bot, state, SUBSCRIPTION_URL)

    assert await state.get_state() is None
    assert "Фото принято" in _texts(session)[-1]
    [row] = ugc_rows(sync_engine, attempt_id)
    assert row.post_url == POST_URL
    assert row.subscription_claimed_url == SUBSCRIPTION_URL


async def test_photo_only_brand_click_goes_through_real_catalog(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    await _register_guest(real_core)

    await _feed_callback(bot, state, "brand:brand_e")  # две активности: клавиатура выбора
    await _feed_callback(bot, state, f"activity:{PHOTO_ONLY_SLUG}")
    assert await state.get_state() == UgcStates.WaitPhoto.state
    attempt_id = (await state.get_data())["attempt_id"]
    await _send_photo(bot, state)

    assert await state.get_state() is None
    assert "Фото принято" in _texts(session)[-1]
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"
    [row] = ugc_rows(sync_engine, attempt_id)
    assert (row.post_url, row.subscription_claimed_url) == (None, None)


async def test_unregistered_guest_gets_channel_not_found_text(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)

    assert await state.get_state() is None
    assert _texts(session)[-1] == ugc.ugc_error_text("channel_not_found")


async def test_guest_without_consent_gets_consent_text(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    await _register_guest(real_core, consent=False)

    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)

    assert await state.get_state() is None
    assert _texts(session)[-1] == ugc.ugc_error_text("consent_required")


async def test_unknown_activity_gets_unavailable_text(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    await _register_guest(real_core)

    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG, slug="no_such_activity")

    assert await state.get_state() is None
    assert _texts(session)[-1] == ugc.ugc_error_text("activity_not_found")


async def test_second_attempt_same_day_gets_conflict_text(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    await _register_guest(real_core)
    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)
    await _send_photo(bot, state)
    assert await state.get_state() is None

    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)

    assert await state.get_state() is None
    assert _texts(session)[-1] == ugc.ugc_error_text("already_passed_today")


async def test_oversized_photo_keeps_attempt_open_and_guest_can_retry(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    """Отказ по размеру приходит до записи: прохождение in_progress, значит FSM сбрасывать нельзя.

    Иначе повторный клик по кнопке упёрся бы в 409 (открытое прохождение держит «раз в день»).
    """
    await _register_guest(real_core)
    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)
    attempt_id = (await state.get_data())["attempt_id"]

    await _send_photo(bot, state, largest=6_000_000)  # лимит активности 5 МиБ

    assert _texts(session)[-1] == ugc.ugc_error_text("file_too_large")
    assert await state.get_state() == UgcStates.WaitPhoto.state
    assert attempt_status(sync_engine, attempt_id) == "in_progress"
    assert ugc_rows(sync_engine, attempt_id) == []

    # Повторный клик по кнопке: ядро отвечает 409, но начатое прохождение не теряется.
    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)
    assert await state.get_state() == UgcStates.WaitPhoto.state
    assert (await state.get_data())["attempt_id"] == attempt_id

    await _send_photo(bot, state)
    assert await state.get_state() is None
    assert attempt_status(sync_engine, attempt_id) == "pending_moderation"
    assert len(ugc_rows(sync_engine, attempt_id)) == 1


async def test_stale_attempt_gets_reopen_text_and_clears_fsm(
    bot: Bot,
    state: FSMContext,
    session: _Session,
    real_core: InternalApiClient,
    sync_engine: Engine,
) -> None:
    """Прохождение уже ушло на модерацию, а в FSM осталось старое ожидание (дубль, перезапуск)."""
    await _register_guest(real_core)
    await _launch_from_db(bot, state, sync_engine, PHOTO_ONLY_SLUG)
    stale_data = await state.get_data()
    await _send_photo(bot, state)
    await state.set_state(UgcStates.WaitPhoto)
    await state.update_data(**stale_data)

    await _send_photo(bot, state)

    assert _texts(session)[-1] == ugc.ugc_error_text("attempt_not_in_progress")
    assert await state.get_state() is None
    assert await state.get_data() == {}
