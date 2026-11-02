"""Фаза 10b-3 (I-2): `/forget` каскад (анонимизация + UGC/FSM hard delete).

Правило 3 (headless-ядро): проверяем функции `request_forget` / `confirm_forget` из
`app/core/personal_data/service.py` на фейковых репозиториях.

Правило 2 (ПДн в логах): все проверки — через возвращаемый `ForgetResult` (счётчики)
и состояние фейковых репо (без прямого обращения к сырым ПДн в выходе).

Инварианты:
* `request_forget` на анонимном госте → `ConflictError('not_registered')`;
* `request_forget` на уже анонимизированном → `ConflictError('already_anonymized')`;
* `confirm_forget` на регистрированном госте: ПДн обнулены, consent revoked, UGC
  удалены, s3_delete джобы в очереди, FSM-строки удалены по user_id;
* повторный `confirm_forget` → `ConflictError('already_anonymized')`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from app.common.errors import ConflictError
from app.core.consent.policy import CURRENT_POLICY_VERSION
from app.core.jobs.types import JobKind
from app.core.personal_data import confirm_forget, request_forget
from app.core.types.dto import UserRef
from app.core.types.enums import ChannelType
from app.core.types.ids import ConsentId, JobId, UserId

NOW = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)


class _FakeUsers:
    """UserRepo fake: регистрация/анонимизация, минимум для теста."""

    def __init__(self) -> None:
        self.records: dict[int, dict[str, Any]] = {}

    def register(
        self,
        user_id: int,
        *,
        first_name: str = "Иван",
        phone: str = "+79001234567",
        is_registered: bool = True,
        anonymized_at: datetime | None = None,
    ) -> None:
        self.records[user_id] = {
            "first_name": first_name,
            "last_name": "Петров",
            "phone": phone,
            "email": f"user{user_id}@example.com",
            "is_registered": is_registered,
            "anonymized_at": anonymized_at,
            "created_at": NOW,
        }

    async def get_by_id(self, user_id: UserId) -> UserRef:
        from app.common.errors import NotFound

        row = self.records.get(int(user_id))
        if row is None:
            raise NotFound("user_not_found")
        return UserRef(
            id=user_id,
            is_registered=bool(row["is_registered"]),
            anonymized_at=row["anonymized_at"],
            created_at=row["created_at"],
        )

    async def anonymize(self, user_id: UserId, now: datetime) -> bool:
        row = self.records.get(int(user_id))
        if row is None or row.get("anonymized_at") is not None:
            return False
        row["first_name"] = None
        row["last_name"] = None
        row["phone"] = None
        row["email"] = None
        row["anonymized_at"] = now
        return True

    # Остальные методы Protocol — не используются в тесте, оставляем no-op заглушки.
    async def insert_blank(self, now: datetime) -> UserRef:  # pragma: no cover
        raise NotImplementedError

    async def touch_last_seen(
        self, user_id: UserId, now: datetime
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def update_pii(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def list_registered(
        self, limit: int, offset: int
    ) -> list[Any]:  # pragma: no cover
        return []

    async def count_registered(self) -> int:  # pragma: no cover
        return 0

    async def get_pii(self, user_id: UserId) -> Any:  # pragma: no cover
        raise NotImplementedError


class _FakeConsents:
    def __init__(self) -> None:
        self.revoked: set[tuple[int, str]] = set()
        self.active: set[tuple[int, str]] = set()

    def accept(self, user_id: int, version: str = CURRENT_POLICY_VERSION) -> None:
        self.active.add((user_id, version))

    async def has_active(self, user_id: UserId, policy_version: str) -> bool:
        return (int(user_id), policy_version) in self.active

    async def insert_active(
        self,
        user_id: UserId,
        policy_version: str,
        text_hash: str,
        channel_type: ChannelType,
        now: datetime,
    ) -> ConsentId:  # pragma: no cover
        return ConsentId(1)

    async def revoke_active(
        self, user_id: UserId, policy_version: str, now: datetime
    ) -> bool:
        key = (int(user_id), policy_version)
        if key in self.active:
            self.active.discard(key)
            self.revoked.add(key)
            return True
        return False


class _FakeUgc:
    """Утиная типизация SqlUgcRepo (Protocol-а нет)."""

    def __init__(self) -> None:
        # {submission_id: (bucket, key, user_id)}
        self.rows: dict[int, tuple[str, str, int]] = {}
        self.deleted_users: list[int] = []

    def add_file(self, submission_id: int, bucket: str, key: str, user_id: int) -> None:
        self.rows[submission_id] = (bucket, key, user_id)

    async def list_files_for_user(self, user_id: int) -> list[tuple[int, str, str]]:
        return [
            (sid, bucket, key)
            for sid, (bucket, key, uid) in self.rows.items()
            if uid == int(user_id)
        ]

    async def delete_for_user(self, user_id: int) -> int:
        before = len(self.rows)
        self.rows = {
            sid: data for sid, data in self.rows.items() if data[2] != int(user_id)
        }
        deleted = before - len(self.rows)
        self.deleted_users.append(int(user_id))
        return deleted


class _FakeTgFsm:
    def __init__(self) -> None:
        # {key: user_id}
        self.rows: dict[str, int | None] = {}
        self.cleaned_users: list[int] = []

    def add(self, key: str, user_id: int | None) -> None:
        self.rows[key] = user_id

    async def delete_by_user(self, user_id: int) -> int:
        before = len(self.rows)
        self.rows = {k: uid for k, uid in self.rows.items() if uid != int(user_id)}
        deleted = before - len(self.rows)
        self.cleaned_users.append(int(user_id))
        return deleted


class _FakeJobs:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, Mapping[str, Any], str | None]] = []
        self._next_id = 1
        self._idem_seen: set[str] = set()

    async def insert(
        self,
        kind: JobKind,
        payload: Mapping[str, Any],
        run_after: datetime | None,
        idempotency_key: str | None,
    ) -> JobId | None:
        if idempotency_key is not None and idempotency_key in self._idem_seen:
            return None
        if idempotency_key is not None:
            self._idem_seen.add(idempotency_key)
        self.jobs.append((kind, dict(payload), idempotency_key))
        job_id = self._next_id
        self._next_id += 1
        return JobId(job_id)

    async def id_by_idempotency_key(
        self, idempotency_key: str
    ) -> JobId | None:  # pragma: no cover
        return None


# ----------------------------------------------------------- request_forget


async def test_request_forget_requires_registered_user() -> None:
    """Анонимный (не зарегистрирован) → ConflictError('not_registered')."""
    users = _FakeUsers()
    users.register(1, is_registered=False)
    with pytest.raises(ConflictError) as exc:
        await request_forget(users, UserId(1))
    assert exc.value.code == "not_registered"


async def test_request_forget_rejects_already_anonymized() -> None:
    """Повторный /forget на уже анонимизированном → already_anonymized."""
    users = _FakeUsers()
    users.register(1, anonymized_at=NOW)
    with pytest.raises(ConflictError) as exc:
        await request_forget(users, UserId(1))
    assert exc.value.code == "already_anonymized"


async def test_request_forget_passes_for_registered_user() -> None:
    """Зарегистрированный, не анонимизированный → без исключений."""
    users = _FakeUsers()
    users.register(1)
    await request_forget(users, UserId(1))


# ----------------------------------------------------------- confirm_forget


async def test_confirm_forget_anonymizes_user_and_revokes_consent() -> None:
    users = _FakeUsers()
    users.register(1)
    consents = _FakeConsents()
    consents.accept(1)
    ugc = _FakeUgc()
    tg_fsm = _FakeTgFsm()
    jobs = _FakeJobs()

    result = await confirm_forget(users, consents, ugc, tg_fsm, jobs, UserId(1), NOW)

    assert result.user_id == 1
    assert users.records[1]["first_name"] is None
    assert users.records[1]["last_name"] is None
    assert users.records[1]["phone"] is None
    assert users.records[1]["email"] is None
    assert users.records[1]["anonymized_at"] == NOW
    assert (1, CURRENT_POLICY_VERSION) in consents.revoked


async def test_confirm_forget_hard_deletes_ugc_and_enqueues_s3_delete() -> None:
    """UGC: enqueue s3_delete на каждый файл + DELETE строк."""
    users = _FakeUsers()
    users.register(1)
    consents = _FakeConsents()
    consents.accept(1)
    ugc = _FakeUgc()
    ugc.add_file(101, "ugc", "a/b.jpg", user_id=1)
    ugc.add_file(102, "ugc", "c/d.jpg", user_id=1)
    ugc.add_file(103, "ugc", "e/f.jpg", user_id=2)  # чужой — не трогаем
    tg_fsm = _FakeTgFsm()
    jobs = _FakeJobs()

    result = await confirm_forget(users, consents, ugc, tg_fsm, jobs, UserId(1), NOW)

    # Два s3_delete джоба с правильными bucket/key и idempotency.
    assert result.s3_delete_jobs_enqueued == 2
    kinds = [job[0] for job in jobs.jobs]
    assert kinds == ["s3_delete", "s3_delete"]
    payloads = sorted((j[1]["bucket"], j[1]["key"]) for j in jobs.jobs)
    assert payloads == [("ugc", "a/b.jpg"), ("ugc", "c/d.jpg")]
    idems = sorted(job[2] for job in jobs.jobs if job[2])
    assert idems == ["forget:101", "forget:102"]

    # UGC-строки удалены для user_id=1, чужая осталась.
    assert result.ugc_submissions_deleted == 2
    assert 103 in ugc.rows
    assert 101 not in ugc.rows and 102 not in ugc.rows


async def test_confirm_forget_hard_deletes_tg_fsm_rows() -> None:
    """FSM: DELETE WHERE user_id = :uid — чужие строки без user_id не трогаются."""
    users = _FakeUsers()
    users.register(1)
    consents = _FakeConsents()
    ugc = _FakeUgc()
    tg_fsm = _FakeTgFsm()
    tg_fsm.add("bot:1:111:::default", user_id=1)
    tg_fsm.add("bot:1:112:::default", user_id=1)
    tg_fsm.add("bot:1:222:::default", user_id=2)
    tg_fsm.add("bot:1:999:::default", user_id=None)  # исторический, без user_id
    jobs = _FakeJobs()

    result = await confirm_forget(users, consents, ugc, tg_fsm, jobs, UserId(1), NOW)

    assert result.tg_fsm_rows_deleted == 2
    remaining = set(tg_fsm.rows.keys())
    assert remaining == {"bot:1:222:::default", "bot:1:999:::default"}


async def test_confirm_forget_idempotent_second_call_returns_already_anonymized() -> None:
    """Повторный confirm_forget → ConflictError('already_anonymized').

    Защита от повторного s3_delete: после первого раза users.anonymize вернёт False.
    """
    users = _FakeUsers()
    users.register(1)
    consents = _FakeConsents()
    consents.accept(1)
    ugc = _FakeUgc()
    tg_fsm = _FakeTgFsm()
    jobs = _FakeJobs()

    await confirm_forget(users, consents, ugc, tg_fsm, jobs, UserId(1), NOW)
    with pytest.raises(ConflictError) as exc:
        await confirm_forget(users, consents, ugc, tg_fsm, jobs, UserId(1), NOW)
    assert exc.value.code == "already_anonymized"


async def test_confirm_forget_no_consent_does_not_fail() -> None:
    """Нет действующего consent (гость успел отозвать раньше) → anonymize всё равно идёт."""
    users = _FakeUsers()
    users.register(1)
    consents = _FakeConsents()  # пустые active
    ugc = _FakeUgc()
    tg_fsm = _FakeTgFsm()
    jobs = _FakeJobs()

    result = await confirm_forget(users, consents, ugc, tg_fsm, jobs, UserId(1), NOW)
    assert result.user_id == 1
    assert users.records[1]["anonymized_at"] == NOW
