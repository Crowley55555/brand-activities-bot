"""Награды: генератор кодов (T6, T7), grant_on_completion, revoke, list_by_user.

ADR-13 (генерация кода + защита от дубля) и ADR-14 (в награде лежат id карточек,
актуальные тексты читаются при показе, soft-deleted карточки остаются в истории).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.common.errors import ConflictError, NotFound, ValidationError
from app.core.rewards import grant_on_completion, list_by_user, revoke
from app.core.rewards.code_generator import generate_code
from app.core.types.dto import GrantSpec
from app.core.types.ids import AttemptId, RewardId, UserId
from tests.conftest import ManualClock
from tests.factories import create_attempt, create_guest
from tests.factories.survey import PROMO_RE, PROMO_TEMPLATE, SURVEY_SLUG, UowFactory

PROMOCODE = GrantSpec(reward_type="promocode", code_template=PROMO_TEMPLATE)


def _source(values: list[str]) -> tuple[Callable[[int], str], list[int]]:
    """random_source из заранее заданных значений + журнал запрошенных длин."""
    calls: list[int] = []
    iterator = iter(values)

    def source(length: int) -> str:
        calls.append(length)
        return next(iterator)

    return source, calls


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def _card_ids(engine: Engine, count: int = 3) -> list[int]:
    rows = _rows(engine, "SELECT id FROM product_recommendations ORDER BY id LIMIT :n", n=count)
    assert len(rows) == count, "сид 0004 не наполнил product_recommendations"
    return [int(r.id) for r in rows]


@pytest.fixture
def restore_cards(sync_engine: Engine) -> Iterator[None]:
    """Тесты ADR-14 правят справочник; возвращаем его в исходное состояние."""
    snapshot = _rows(
        sync_engine,
        "SELECT id, product_name, caption, product_url, is_active, deleted_at "
        "FROM product_recommendations",
    )
    yield
    with sync_engine.begin() as conn:
        for row in snapshot:
            conn.execute(
                text(
                    "UPDATE product_recommendations SET product_name = :name, "
                    "caption = :caption, product_url = :url, is_active = :active, "
                    "deleted_at = :deleted WHERE id = :id"
                ),
                {
                    "id": row.id,
                    "name": row.product_name,
                    "caption": row.caption,
                    "url": row.product_url,
                    "active": row.is_active,
                    "deleted": row.deleted_at,
                },
            )


# ---------------------------------------------------------------- T6


def test_code_generator_produces_unique_codes() -> None:
    codes = [generate_code("BRAND_A-{random8}") for _ in range(1000)]

    assert len(set(codes)) == 1000
    assert all(PROMO_RE.fullmatch(code) for code in codes)
    assert re.fullmatch(r"BRAND_A-[A-Z0-9]{8}", codes[0])


def test_code_generator_uses_whole_alphabet_not_only_letters() -> None:
    """Регрессия: base64url даёт '-' и '_'; нужен чистый [A-Z0-9] (ADR-13)."""
    joined = "".join(generate_code("{random8}") for _ in range(500))

    assert re.fullmatch(r"[A-Z0-9]+", joined)
    assert any(ch.isdigit() for ch in joined)
    assert any(ch.isalpha() for ch in joined)


@pytest.mark.parametrize(
    ("template", "pattern"),
    [
        ("BRAND_A-{random8}", r"BRAND_A-[A-Z0-9]{8}"),
        ("X-{random4}", r"X-[A-Z0-9]{4}"),
        ("{random12}", r"[A-Z0-9]{12}"),
        ("PRE-{random6}-SUF", r"PRE-[A-Z0-9]{6}-SUF"),
    ],
)
def test_code_generator_length_comes_from_placeholder(template: str, pattern: str) -> None:
    assert re.fullmatch(pattern, generate_code(template))


def test_code_generator_calls_random_source_with_placeholder_length() -> None:
    source, calls = _source(["QWERTY12"])

    assert generate_code("BRAND_A-{random8}", source) == "BRAND_A-QWERTY12"
    assert calls == [8]


@pytest.mark.parametrize(
    "template", ["BRAND_A", "BRAND_A-{random}", "BRAND_A-{random0}", "{random8}{random8}", ""]
)
def test_code_generator_rejects_templates_without_exactly_one_placeholder(template: str) -> None:
    with pytest.raises(ValidationError) as exc:
        generate_code(template)

    assert exc.value.code == "code_template_invalid"


def test_code_generator_rejects_source_with_wrong_length() -> None:
    with pytest.raises(ValidationError) as exc:
        generate_code("BRAND_A-{random8}", lambda n: "SHORT")

    assert exc.value.code == "code_source_invalid"


# ---------------------------------------------------------------- T7


async def test_code_collision_retry(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    """3 коллизии подряд, 4-я попытка вставляется (ADR-13: до 5 попыток)."""
    first = create_guest(sync_engine, "1001")
    second = create_guest(sync_engine, "1002")
    attempt_one = create_attempt(sync_engine, first, SURVEY_SLUG)
    attempt_two = create_attempt(sync_engine, second, SURVEY_SLUG)
    # 1-й вызов: AAAAAAAA проходит. 2-й вызов: три раза AAAAAAAA (коллизии),
    # четвёртая попытка отдаёт BBBBBBBB.
    source, calls = _source(["AAAAAAAA", "AAAAAAAA", "AAAAAAAA", "AAAAAAAA", "BBBBBBBB"])

    async with uow_factory() as uow:
        issued_one = await grant_on_completion(
            uow.repositories.rewards,
            AttemptId(attempt_one),
            [PROMOCODE],
            manual_clock.now(),
            source,
        )
    async with uow_factory() as uow:
        issued_two = await grant_on_completion(
            uow.repositories.rewards,
            AttemptId(attempt_two),
            [PROMOCODE],
            manual_clock.now(),
            source,
        )

    assert issued_one[0].code == "BRAND_A-AAAAAAAA"
    assert issued_two[0].code == "BRAND_A-BBBBBBBB"
    assert len(calls) == 5
    stored = {
        r.attempt_id: r.code for r in _rows(sync_engine, "SELECT attempt_id, code FROM rewards")
    }
    assert stored == {attempt_one: "BRAND_A-AAAAAAAA", attempt_two: "BRAND_A-BBBBBBBB"}


async def test_code_collision_gives_up_after_five_attempts(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    first = create_guest(sync_engine, "1001")
    second = create_guest(sync_engine, "1002")
    attempt_one = create_attempt(sync_engine, first, SURVEY_SLUG)
    attempt_two = create_attempt(sync_engine, second, SURVEY_SLUG)
    source, calls = _source(["AAAAAAAA"] * 6)

    async with uow_factory() as uow:
        await grant_on_completion(
            uow.repositories.rewards,
            AttemptId(attempt_one),
            [PROMOCODE],
            manual_clock.now(),
            source,
        )
    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await grant_on_completion(
                uow.repositories.rewards,
                AttemptId(attempt_two),
                [PROMOCODE],
                manual_clock.now(),
                source,
            )

    assert exc.value.code == "code_collision"
    assert len(calls) == 1 + 5  # одна успешная + ровно пять неудачных попыток
    assert [r.attempt_id for r in _rows(sync_engine, "SELECT attempt_id FROM rewards")] == [
        attempt_one
    ]


# --------------------------------------------------- grant_on_completion


async def test_grant_issues_each_spec_and_returns_issued_rewards(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    guest = create_guest(sync_engine)
    attempt = create_attempt(sync_engine, guest, SURVEY_SLUG)
    ids = _card_ids(sync_engine)

    async with uow_factory() as uow:
        issued = await grant_on_completion(
            uow.repositories.rewards,
            AttemptId(attempt),
            [
                GrantSpec(reward_type="recommendation_set", product_recommendation_ids=ids),
                PROMOCODE,
            ],
            manual_clock.now(),
        )

    assert [r.reward_type for r in issued] == ["recommendation_set", "promocode"]
    assert issued[0].product_recommendation_ids == tuple(ids)
    assert issued[0].code is None
    assert PROMO_RE.fullmatch(issued[1].code or "")
    assert issued[1].product_recommendation_ids is None
    assert all(r.attempt_id == attempt for r in issued)
    assert all(r.issued_at == manual_clock.now() for r in issued)
    rows = _rows(sync_engine, "SELECT reward_type FROM rewards ORDER BY id")
    assert [r.reward_type for r in rows] == ["recommendation_set", "promocode"]


async def test_duplicate_type_in_one_call_rolls_everything_back(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    guest = create_guest(sync_engine)
    attempt = create_attempt(sync_engine, guest, SURVEY_SLUG)

    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await grant_on_completion(
                uow.repositories.rewards,
                AttemptId(attempt),
                [PROMOCODE, PROMOCODE],
                manual_clock.now(),
            )

    assert exc.value.code == "duplicate_grant"
    assert _rows(sync_engine, "SELECT id FROM rewards") == []


@pytest.mark.parametrize(
    "spec",
    [
        GrantSpec(reward_type="promocode"),  # нет шаблона кода
        GrantSpec(reward_type="voucher"),  # нет шаблона кода
        GrantSpec(reward_type="recommendation_set"),  # нет id карточек
        GrantSpec(reward_type="recommendation_set", product_recommendation_ids=[]),
        GrantSpec(reward_type="wheel_prize"),  # нет wheel_prize_id
        GrantSpec(
            reward_type="recommendation_set",
            product_recommendation_ids=[1],
            code_template="X-{random8}",
        ),
        GrantSpec(reward_type="leaflet", code_template="X-{random8}"),
        GrantSpec(
            reward_type="promocode", code_template="X-{random8}", product_recommendation_ids=[1]
        ),
    ],
)
async def test_grant_rejects_malformed_spec_before_touching_db(
    spec: GrantSpec, uow_factory: UowFactory, manual_clock: ManualClock
) -> None:
    """Форму GrantSpec проверяет ядро, а не CHECK базы (там вышел бы IntegrityError)."""
    with pytest.raises(ValidationError) as exc:
        async with uow_factory() as uow:
            await grant_on_completion(
                uow.repositories.rewards, AttemptId(1), [spec], manual_clock.now()
            )

    assert exc.value.code == "grant_spec_invalid"


async def test_grant_with_empty_list_is_rejected(
    uow_factory: UowFactory, manual_clock: ManualClock
) -> None:
    with pytest.raises(ValidationError) as exc:
        async with uow_factory() as uow:
            await grant_on_completion(
                uow.repositories.rewards, AttemptId(1), [], manual_clock.now()
            )

    assert exc.value.code == "grant_spec_invalid"


# ------------------------------------------------------------- revoke


async def test_revoke_marks_reward_and_cannot_be_brand_bed(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    guest = create_guest(sync_engine)
    attempt = create_attempt(sync_engine, guest, SURVEY_SLUG)
    async with uow_factory() as uow:
        issued = await grant_on_completion(
            uow.repositories.rewards, AttemptId(attempt), [PROMOCODE], manual_clock.now()
        )
    reward_id = RewardId(issued[0].id)

    async with uow_factory() as uow:
        await revoke(uow.repositories.rewards, reward_id, "выдан по ошибке", manual_clock.now())

    row = _rows(sync_engine, "SELECT revoked_at, revoke_reason FROM rewards")[0]
    assert row.revoked_at is not None
    assert row.revoke_reason == "выдан по ошибке"
    with pytest.raises(ConflictError) as exc:
        async with uow_factory() as uow:
            await revoke(uow.repositories.rewards, reward_id, "ещё раз", manual_clock.now())
    assert exc.value.code == "already_revoked"


async def test_revoke_unknown_reward_is_not_found(
    uow_factory: UowFactory, manual_clock: ManualClock, clean_db: None
) -> None:
    with pytest.raises(NotFound) as exc:
        async with uow_factory() as uow:
            await revoke(uow.repositories.rewards, RewardId(999_999), "x", manual_clock.now())

    assert exc.value.code == "reward_not_found"


# ------------------------------------------------------- list_by_user


async def test_list_by_user_returns_only_own_rewards_newest_first(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
) -> None:
    mine = create_guest(sync_engine, "1001")
    other = create_guest(sync_engine, "1002")
    my_attempt = create_attempt(sync_engine, mine, SURVEY_SLUG)
    other_attempt = create_attempt(sync_engine, other, SURVEY_SLUG)
    ids = _card_ids(sync_engine, 2)
    async with uow_factory() as uow:
        await grant_on_completion(
            uow.repositories.rewards,
            AttemptId(my_attempt),
            [
                GrantSpec(reward_type="recommendation_set", product_recommendation_ids=ids),
                PROMOCODE,
            ],
            manual_clock.now(),
        )
        await grant_on_completion(
            uow.repositories.rewards, AttemptId(other_attempt), [PROMOCODE], manual_clock.now()
        )

    async with uow_factory() as uow:
        r = uow.repositories
        history = await list_by_user(r.rewards, r.catalog, UserId(mine.user_id))

    assert {item.attempt_id for item in history} == {my_attempt}
    assert {item.reward_type for item in history} == {"recommendation_set", "promocode"}
    # Одинаковый issued_at: порядок задаёт id по убыванию (новее - выше).
    assert [item.id for item in history] == sorted((item.id for item in history), reverse=True)


async def test_list_by_user_shows_current_card_text_and_soft_deleted_cards(
    uow_factory: UowFactory,
    sync_engine: Engine,
    manual_clock: ManualClock,
    clean_db: None,
    restore_cards: None,
) -> None:
    """ADR-14: в награде только id; текст актуальный; удалённая карточка остаётся в истории."""
    guest = create_guest(sync_engine)
    attempt = create_attempt(sync_engine, guest, SURVEY_SLUG)
    first_id, second_id, third_id = _card_ids(sync_engine, 3)
    async with uow_factory() as uow:
        await grant_on_completion(
            uow.repositories.rewards,
            AttemptId(attempt),
            [
                GrantSpec(
                    reward_type="recommendation_set",
                    product_recommendation_ids=[first_id, second_id, third_id],
                )
            ],
            manual_clock.now(),
        )
    stored = _rows(sync_engine, "SELECT product_recommendation_ids FROM rewards")[0]
    assert stored.product_recommendation_ids == [first_id, second_id, third_id]

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE product_recommendations "
                "SET product_name = 'Новое название', product_url = 'https://example.com/moved' "
                "WHERE id = :id"
            ),
            {"id": first_id},
        )
        conn.execute(
            text("UPDATE product_recommendations SET deleted_at = now() WHERE id = :id"),
            {"id": second_id},
        )
        conn.execute(
            text("UPDATE product_recommendations SET is_active = FALSE WHERE id = :id"),
            {"id": third_id},
        )

    async with uow_factory() as uow:
        r = uow.repositories
        history = await list_by_user(r.rewards, r.catalog, UserId(guest.user_id))

    assert len(history) == 1
    cards = history[0].recommendations
    assert [card.id for card in cards] == [first_id, second_id, third_id]  # порядок выдачи
    assert cards[0].product_name == "Новое название"
    assert cards[0].product_url == "https://example.com/moved"
    assert cards[1].product_name  # soft-deleted всё ещё читается
    assert cards[2].product_name  # деактивированная - тоже


async def test_list_by_user_for_guest_without_rewards_is_empty(
    uow_factory: UowFactory, sync_engine: Engine, clean_db: None
) -> None:
    guest = create_guest(sync_engine)

    async with uow_factory() as uow:
        r = uow.repositories
        assert await list_by_user(r.rewards, r.catalog, UserId(guest.user_id)) == []
