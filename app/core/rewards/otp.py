"""OTP-погашение именных наград (фаза 10b-1, C-1).

Канон: 02 §7 (OTP у Reward), 03 §1 (rewards), 04 §1.2 (сценарий «подойду сейчас»),
09 §3 (риск №3, вариант B).

Три операции, все идёмпотентные там, где это уместно:

* `request_otp` — гость в боте жмёт «подойду сейчас», ядро выдаёт 6-значный OTP со
  сроком жизни `ttl_minutes` минут (по умолчанию 5, из `REWARDS_OTP_TTL_MINUTES`).
  Повторный вызов на ту же награду перезаписывает OTP и `expires_at`: старый
  становится недействительным. Это закрывает «гость забыл показать, OTP истёк».
* `find_by_otp` — админ ввёл OTP → ядро отдаёт карточку гостя (маскированное имя
  и telegram_username; телефон/email не показываем — правило 9, у стойки они не
  нужны). Истёк / уже погашен → `None`; гость анонимизирован → `ConflictError`.
* `redeem_with_otp` — атомарный `UPDATE ... SET redeemed_at=now()`,
  `redeemed_by_admin_id=?`, `redeem_otp=NULL`, `redeem_otp_expires_at=NULL`
  `WHERE redeem_otp=? AND redeem_otp_expires_at > now() AND redeemed_at IS NULL`.
  Если 0 строк затронуто — админ видит «код не найден или просрочен».

Правило 2 (ПДн в логах): ни один из этих модулей не логирует `otp`, `first_name`,
`last_name`, `phone`, `email`, `telegram_username`. Правило 9 (ПДн наружу):
карточка с ПДн ходит только внутри ядра / админки / HMAC-канала для tg_worker.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.common.errors import ConflictError
from app.core.rewards.repository import RewardRepo
from app.core.types.enums import RewardType
from app.core.types.ids import AdminId, RewardId, UserId

# Канон 02 §7: «6 цифр или 6 букв+цифр без похожих символов». Выбор — 6 цифр: проще
# продиктовать голосом у стойки, нет коллизий 0/O, 1/I/l. См. промт §3.7.
OTP_LENGTH: Final[int] = 6
_OTP_DIGITS: Final[str] = "0123456789"

# fix-1 I-1 (10b-1): коллизия partial unique `rewards_active_otp` — не 500. При ~10⁴
# живых OTP вероятность конфликта на `secrets.choice('0123456789')×6` ≈ 10⁻², после
# 5 попыток падение в `ConflictError('otp_collision')` ещё остаётся осязаемой
# верхней границей, но «кликнул ещё раз» на фронте закроет кейс. Паттерн тот же,
# что `MAX_CODE_ATTEMPTS=5` для коллизии `rewards_code` в
# `app/core/rewards/service.py`.
MAX_OTP_ATTEMPTS: Final[int] = 5

# Награды, у которых есть именной цикл погашения: канон 02 §7, 09 §3.
# `wheel_prize` в фазе 10b-1 не включён: текущая `wheel_prizes` не различает
# именные и предъявительские призы (см. промт §3.7, known-limit).
NAMED_REWARD_TYPES: Final[frozenset[RewardType]] = frozenset({"voucher", "physical_gift"})

# Источник случайных цифр (6 символов за один вызов). Тесты подменяют на детерминированный.
OtpSource = Callable[[], str]


def default_otp_source() -> str:
    """Шесть цифр из криптостойкого источника (`secrets.choice`)."""
    return "".join(secrets.choice(_OTP_DIGITS) for _ in range(OTP_LENGTH))


@dataclass(frozen=True)
class OtpIssued:
    """Выданный OTP: сам код и момент истечения."""

    otp: str
    expires_at: datetime


@dataclass(frozen=True)
class OtpCard:
    """Карточка гостя под OTP для админа у стойки.

    `first_name_masked`/`last_name_masked` — маскированные имена вида «И***н» или
    «И.» для коротких (см. `_mask_name`). `telegram_username` — публичный ник в
    Telegram без маски (по канону 02 §1 отдельно от других ПДн, у стойки нужен).
    Телефон/email **не** отдаём: правило 9 (у стойки они не нужны для операции).
    """

    reward_id: RewardId
    reward_type: RewardType
    title: str | None
    code: str | None
    first_name_masked: str
    last_name_masked: str
    telegram_username: str | None


@dataclass(frozen=True)
class OtpCardRow:
    """Промежуточный DTO репозитория: сырые ПДн плюс флаг анонимизации."""

    reward_id: RewardId
    reward_type: RewardType
    title: str | None
    code: str | None
    user_id: UserId
    first_name: str | None
    last_name: str | None
    telegram_username: str | None
    anonymized_at: datetime | None


@dataclass(frozen=True)
class RewardOtpEligibility:
    """Что ядро знает о награде перед выпуском OTP."""

    reward_type: RewardType
    is_bearer: bool
    redeemed_at: datetime | None
    revoked_at: datetime | None


def _mask_name(value: str | None) -> str:
    """«Иван» → «И***н»; «Ян» → «Я.»; пусто → «—».

    Способ по промту §3.8: первая и последняя буква имени, остальное — «*»; имя
    короче 3 букв — «И.». NULL / пусто — «—», чтобы шаблон не отрисовывал пробел.
    """
    if not value:
        return "—"
    text = value.strip()
    if not text:
        return "—"
    if len(text) < 3:
        return f"{text[0]}."
    return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"


def _mask_card(row: OtpCardRow) -> OtpCard:
    return OtpCard(
        reward_id=row.reward_id,
        reward_type=row.reward_type,
        title=row.title,
        code=row.code,
        first_name_masked=_mask_name(row.first_name),
        last_name_masked=_mask_name(row.last_name),
        telegram_username=row.telegram_username,
    )


async def request_otp(
    repo: RewardRepo,
    reward_id: RewardId,
    user_id: UserId,
    now: datetime,
    ttl_minutes: int,
    otp_source: OtpSource | None = None,
) -> OtpIssued:
    """Запрос OTP на именную награду `reward_id`, принадлежащую `user_id`.

    Ошибки:
    * `ConflictError('reward_not_found')` — награда не существует или другого гостя;
    * `ConflictError('already_redeemed')` — redeemed_at IS NOT NULL;
    * `ConflictError('reward_revoked')` — revoked_at IS NOT NULL;
    * `ConflictError('reward_not_eligible')` — тип не из `NAMED_REWARD_TYPES`
      (promocode, recommendation_set, leaflet, wheel_prize) или `is_bearer=true`;
    * `ConflictError('otp_collision')` — после `MAX_OTP_ATTEMPTS` подряд OTP
      совпадал с живым OTP на другой награде (fix-1 I-1, 10b-1).

    На успехе: UPDATE пишет OTP и `expires_at`; старый OTP (если был) перетирается.
    """
    eligibility = await repo.get_otp_eligibility(reward_id, user_id)
    if eligibility is None:
        raise ConflictError("reward_not_found")
    if eligibility.redeemed_at is not None:
        raise ConflictError("already_redeemed")
    if eligibility.revoked_at is not None:
        raise ConflictError("reward_revoked")
    if eligibility.is_bearer or eligibility.reward_type not in NAMED_REWARD_TYPES:
        raise ConflictError("reward_not_eligible")

    source = otp_source if otp_source is not None else default_otp_source
    expires_at = now + timedelta(minutes=ttl_minutes)
    # fix-1 I-1: ретрай на коллизию partial unique `rewards_active_otp`. `set_otp`
    # сам отвечает `False` если встретил чужой живой OTP с тем же значением —
    # IntegrityError не возбуждается (атомарный `AND NOT EXISTS` в UPDATE),
    # транзакция не ломается, цикл делает новый OTP и повторяет.
    for _ in range(MAX_OTP_ATTEMPTS):
        otp = source()
        if await repo.set_otp(reward_id, otp, expires_at):
            return OtpIssued(otp=otp, expires_at=expires_at)
    raise ConflictError("otp_collision")


async def find_by_otp(
    repo: RewardRepo, otp: str, now: datetime
) -> OtpCard | None:
    """Карточка гостя под OTP; None если не найден / истёк / уже погашен.

    `ConflictError('user_anonymized')` — OTP активный, но гость /forget-нул
    регистрацию между выдачей и погашением. Админ видит ту же форму и понимает,
    что выдача невозможна (физический подарок у стойки без карточки — нельзя).
    """
    row = await repo.find_card_by_otp(otp, now)
    if row is None:
        return None
    if row.anonymized_at is not None:
        raise ConflictError("user_anonymized")
    return _mask_card(row)


async def redeem_with_otp(
    repo: RewardRepo,
    otp: str,
    admin_id: AdminId,
    now: datetime,
    reward_id: RewardId | None = None,
) -> RewardId:
    """Атомарное погашение по OTP.

    Если `reward_id` задан, ядро гасит именно эту награду только при точном
    совпадении её OTP (`redeem_by_otp_and_id`). Это закрывает UX-подделку
    `{reward_id}` в URL админ-формы: админ открыл карточку A, набрал OTP B,
    нажал «Погасил» — ядро не гасит B и возвращает `ConflictError('otp_mismatch')`
    (fix-1 I-2, 10b-1). Если `reward_id=None` — старое поведение
    `redeem_by_otp` для будущих клиентов без карточки.

    Ошибки:
    * `ConflictError('otp_not_found')` — OTP нет, истёк или награда уже погашена;
    * `ConflictError('otp_mismatch')` — передан `reward_id`, но OTP принадлежит
      другой награде (эта и не найдена, или OTP валиден, но не этот).

    На успехе возвращает `reward_id`; `redeem_otp` и `redeem_otp_expires_at`
    становятся NULL (чтобы unique index не держал погашенные коды).
    """
    if reward_id is None:
        found = await repo.redeem_by_otp(otp, admin_id, now)
        if found is None:
            raise ConflictError("otp_not_found")
        return found
    # Чтобы отличить «OTP не существует вовсе» от «OTP есть, но на другой награде»,
    # сначала смотрим карточку по OTP: если нашли карточку с другим id — mismatch.
    card = await repo.find_card_by_otp(otp, now)
    if card is None:
        raise ConflictError("otp_not_found")
    if card.reward_id != reward_id:
        raise ConflictError("otp_mismatch")
    ok = await repo.redeem_by_otp_and_id(otp, reward_id, admin_id, now)
    if not ok:
        # Строка между find_card_by_otp и UPDATE: мы увидели совпадающий id,
        # но UPDATE нулевой — значит кто-то успел погасить/отозвать параллельно.
        raise ConflictError("otp_not_found")
    return reward_id
