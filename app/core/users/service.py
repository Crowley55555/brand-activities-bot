from __future__ import annotations

from datetime import datetime

from app.common.errors import ConflictError
from app.core.audit.repository import AuditRepo
from app.core.audit.service import log_admin_action
from app.core.consent.repository import ConsentRepo
from app.core.consent.service import require_consent
from app.core.types.dto import UserListRow, UserRef
from app.core.types.ids import AdminId, UserId
from app.core.users.repository import UserRepo


async def insert_blank(repo: UserRepo, now: datetime) -> UserRef:
    return await repo.insert_blank(now)


async def get_profile(repo: UserRepo, user_id: UserId) -> UserRef:
    return await repo.get_by_id(user_id)


async def touch_last_seen(
    repo: UserRepo, user_id: UserId, now: datetime
) -> None:
    await repo.touch_last_seen(user_id, now)


async def update_pii(
    users: UserRepo,
    consents: ConsentRepo,
    user_id: UserId,
    first_name: str,
    last_name: str,
    phone_e164: str,
    email_lower: str,
    now: datetime,
) -> UserRef:
    """Записать ПДн пользователя.

    Правило 1 (consent-gate): сначала guard require_consent, затем UPDATE.
    Если действующего согласия нет — ConflictError('consent_required').
    """
    await require_consent(consents, user_id)
    return await users.update_pii(
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        phone_e164=phone_e164,
        email_lower=email_lower,
        now=now,
    )


async def list_users_page(
    users: UserRepo, limit: int, offset: int
) -> tuple[list[UserListRow], int]:
    """Страница зарегистрированных пользователей и общее число (ПДн сырые)."""
    rows = await users.list_registered(limit, offset)
    return rows, await users.count_registered()


async def reveal_user_phone(
    users: UserRepo,
    audit: AuditRepo,
    admin_id: AdminId,
    user_id: UserId,
    now: datetime,
) -> str:
    """Телефон пользователя для админа; журнал пишется только при успехе.

    Анонимизированный - ConflictError('user_anonymized') из репо, до записи в журнал.
    details пуст: телефон в журнал не попадает (правило 9).
    """
    pii = await users.get_pii(user_id)
    if pii.phone is None:
        raise ConflictError("phone_missing")
    await log_admin_action(audit, admin_id, "reveal_user_phone", "users", int(user_id), now)
    return pii.phone
