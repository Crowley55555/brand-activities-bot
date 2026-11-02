from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from app.core.types.dto import RewardIssued, RewardListRow
from app.core.types.enums import RewardType
from app.core.types.ids import AdminId, AttemptId, RewardId, UserId

if TYPE_CHECKING:
    from app.core.rewards.otp import OtpCardRow, RewardOtpEligibility

RewardStatusFilter = Literal["issued", "redeemed", "revoked", "all"]


@dataclass(frozen=True)
class ActiveNamedReward:
    """Строка «моих именных активных наград» для tg_worker (`/my`).

    ПДн тут нет (title — название ваучера, не имя гостя). Код ваучера допустимо
    показать, но tg_worker его в UI «мои награды» не рисует: гость всё равно
    гасит по OTP, а не по коду (это весь смысл фазы 10b-1).
    """

    reward_id: RewardId
    reward_type: RewardType
    title: str | None
    code: str | None
    issued_at: datetime


@runtime_checkable
class RewardRepo(Protocol):
    async def insert(
        self,
        attempt_id: AttemptId,
        reward_type: RewardType,
        code: str | None,
        product_recommendation_ids: Sequence[int] | None,
        wheel_prize_id: int | None,
        title: str | None,
        issued_at: datetime,
    ) -> RewardIssued | None:
        """`INSERT ... ON CONFLICT DO NOTHING RETURNING`.

        None - сработал любой из уникальных индексов: rewards_per_attempt_type
        (attempt_id, reward_type) или rewards_code (code). Какой именно - выясняет
        вызывающий через `exists`. Исключений на конфликте нет, транзакция остаётся
        рабочей, поэтому ретрай на коллизию кода идёт в той же транзакции.
        """
        ...

    async def exists(self, attempt_id: AttemptId, reward_type: RewardType) -> bool: ...

    async def get(self, reward_id: RewardId) -> RewardIssued | None: ...

    async def revoke(self, reward_id: RewardId, reason: str, revoked_at: datetime) -> bool:
        """True - награда отозвана сейчас; False - нет такой или уже отозвана."""
        ...

    async def list_by_user(self, user_id: UserId) -> list[RewardIssued]:
        """Все награды гостя (в т.ч. отозванные), новые первыми. Без карточек."""
        ...

    async def list_for_attempt(self, attempt_id: AttemptId) -> list[RewardIssued]:
        """Все награды прохождения по порядку выдачи (notify_reward берёт их все, Q2 ревью 3b)."""
        ...

    async def list_by_ids(self, reward_ids: Sequence[int]) -> list[RewardIssued]:
        """Награды по id (рендер текста в outbox, ADR-31/32). Отсутствующие id просто не придут."""
        ...

    async def list_filtered(
        self,
        reward_types: Sequence[RewardType],
        status: RewardStatusFilter,
        activity_slug: str | None,
        limit: int,
        offset: int,
    ) -> list[RewardListRow]:
        """Список наград для админки, новые первыми; активность и бренд - через JOIN."""
        ...

    async def count_filtered(
        self,
        reward_types: Sequence[RewardType],
        status: RewardStatusFilter,
        activity_slug: str | None,
    ) -> int:
        """Число наград под те же фильтры (для пагинации)."""
        ...

    # ----- OTP-погашение именных наград (фаза 10b-1, канон 02 §7 / 03 §1 / 04 §1.2) -----

    async def get_otp_eligibility(
        self, reward_id: RewardId, user_id: UserId
    ) -> RewardOtpEligibility | None:
        """Что нужно знать перед выпуском OTP: тип, is_bearer, статусы погашения/отзыва.

        None - награды нет или она принадлежит другому гостю (reward.attempt.user_id).
        Правило 9: карточка со статусами здесь не раскрывает ПДн владельца.
        """
        ...

    async def set_otp(
        self, reward_id: RewardId, otp: str, expires_at: datetime
    ) -> bool:
        """UPDATE SET redeem_otp=?, redeem_otp_expires_at=? WHERE id=?
        AND NOT EXISTS (другой живой OTP = ?). RETURNING id.

        True - OTP записан; False - в системе уже есть живой OTP с таким
        значением на другой награде (коллизия partial unique `rewards_active_otp`).
        Core-цикл (`request_otp`) повторяет попытку с новым OTP; после
        `MAX_OTP_ATTEMPTS` неудач — `ConflictError('otp_collision')`.

        Проверку (`is_bearer=false`, `redeemed_at IS NULL`, `revoked_at IS NULL`)
        делает `request_otp` в core через `get_otp_eligibility`; CHECK в БД —
        второй рубеж на случай гонок.
        """
        ...

    async def find_card_by_otp(
        self, otp: str, now: datetime
    ) -> OtpCardRow | None:
        """Карточка владельца по активному OTP.

        Возвращает `None` если OTP нет, он истёк (`redeem_otp_expires_at <= now`)
        или уже погашен (`redeemed_at IS NOT NULL`); если OTP активен, но гость
        анонимизирован — отдаём строку с `anonymized_at IS NOT NULL`, service сам
        бросит `ConflictError('user_anonymized')`.
        """
        ...

    async def redeem_by_otp(
        self, otp: str, admin_id: AdminId, now: datetime
    ) -> RewardId | None:
        """Атомарное погашение: UPDATE SET redeemed_at=now, redeemed_by_admin_id=?,
        redeem_otp=NULL, redeem_otp_expires_at=NULL
        WHERE redeem_otp=? AND redeem_otp_expires_at > now AND redeemed_at IS NULL
        AND is_bearer = false
        RETURNING id.

        None - 0 строк затронуто (нет такого OTP / истёк / уже погашен).
        """
        ...

    async def redeem_by_otp_and_id(
        self, otp: str, reward_id: RewardId, admin_id: AdminId, now: datetime
    ) -> bool:
        """Как `redeem_by_otp`, но с дополнительным `AND id = :rid` в WHERE.

        True - 1 строка затронута, награда `reward_id` погашена этим OTP;
        False - 0 строк: OTP валиден, но принадлежит ДРУГОЙ награде (admin
        открыл карточку A, набрал OTP B). Core-функция `redeem_with_otp` на
        False бросает `ConflictError('otp_mismatch')` — защита от UX-ошибок
        и подделки `{reward_id}` в URL админ-формы (fix-1 I-2, 10b-1).
        """
        ...

    async def list_active_named_rewards_by_user(
        self, user_id: UserId
    ) -> list[ActiveNamedReward]:
        """Именные (`voucher`, `physical_gift`) не погашенные и не отозванные.

        Используется `/my` в tg_worker: гость выбирает, по какой награде получить
        OTP. Сортировка - новые первыми, тот же критерий, что `list_by_user`.
        """
        ...
