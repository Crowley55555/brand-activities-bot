"""/forget каскад (фаза 10b-3, I-2): анонимизация ПДн + удаление UGC и FSM.

Канон 02 §9 и 08 §335 (`/forget` → анонимизация + каскад на UGC/FSM); правило 9
(ПДн не наружу): анонимизация делается только в нашей БД, UGC-файлы удаляются
через `s3_delete` джоб (ADR-28). Правило 3 (headless-ядро): tg-handler зовёт
HTTP-эндпоинт, тот — функции отсюда.

Операции:
* `request_forget(users_repo, user_id)` — проверяет, что пользователь действительно
  зарегистрирован и не анонимизирован: если нет — `ConflictError('not_registered')`
  или `ConflictError('already_anonymized')`. **Consent-gate (правило 1) здесь НЕ
  применяется**: `/forget` — акт отзыва согласия, его требование означало бы, что
  гость, уже отозвавший consent, больше не может удалить свои данные (противоречит
  152-ФЗ).
* `confirm_forget(repos, jobs_repo, user_id, now)` — в одной транзакции:
  1. `users.anonymize` — обнуляет first_name/last_name/phone/email, ставит anonymized_at;
  2. `consents.revoke_active` — отзывает действующее согласие (если было);
  3. читает `ugc_submissions` гостя с файлами, ставит на каждый `s3_delete` джоб
     с idempotency_key `forget:<submission_id>` (повтор безопасен);
  4. `ugc_submissions.delete_for_user` — hard delete всех строк гостя;
  5. `tg_fsm_state.delete_by_user` — hard delete всех FSM-строк гостя (миграция 0021).
  Возвращает `ForgetResult` для логирования количества (без ПДн).

Повторный `confirm_forget` → `ConflictError('already_anonymized')`: `users.anonymize`
вернёт False, остальные шаги не выполняются (чтобы не ставить лишние s3_delete).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.common.errors import ConflictError, NotFound
from app.core.consent.policy import CURRENT_POLICY_VERSION
from app.core.consent.repository import ConsentRepo
from app.core.jobs.repository import JobRepo
from app.core.jobs.service import enqueue
from app.core.types.ids import UserId
from app.core.users.repository import UserRepo


@dataclass(frozen=True)
class ForgetResult:
    """Результат confirm_forget для логов и ответа клиенту (без ПДн)."""

    user_id: int
    ugc_submissions_deleted: int
    tg_fsm_rows_deleted: int
    s3_delete_jobs_enqueued: int


async def request_forget(users: UserRepo, user_id: UserId) -> None:
    """Guard: пользователь зарегистрирован и не анонимизирован.

    Канон 02 §9: `/forget` для анонимного гостя не имеет смысла — ему нечего удалять.
    Повторный вызов на уже анонимизированном → `ConflictError('already_anonymized')`.
    """
    try:
        user = await users.get_by_id(user_id)
    except NotFound:
        # Гостя нет в БД — он не зарегистрирован (и даже не создавал запись).
        raise ConflictError("not_registered") from None
    if not user.is_registered:
        raise ConflictError("not_registered")
    if user.anonymized_at is not None:
        raise ConflictError("already_anonymized")


async def confirm_forget(
    users: UserRepo,
    consents: ConsentRepo,
    ugc_repo: object,  # SqlUgcRepo — Protocol-а нет
    tg_fsm_repo: object,  # TgFsmStateRepo
    jobs: JobRepo,
    user_id: UserId,
    now: datetime,
) -> ForgetResult:
    """Каскадный `/forget`: анонимизация + hard delete UGC/FSM + enqueue s3_delete.

    Правило 1 (consent-gate): не применяется (см. docstring модуля).
    Правило 2 (ПДн в логах): результат содержит только счётчики, не ПДн.
    """
    anonymized = await users.anonymize(user_id, now)
    if not anonymized:
        # Проверка «уже анонимизирован» — повтор идемпотентен для гостя, но мы
        # хотим чёткий сигнал об этом, иначе клиент не поймёт результат.
        raise ConflictError("already_anonymized")

    # Отзываем действующее согласие (если было). revoke_active молча возвращает False,
    # если согласия нет (например, оно было отозвано ранее) — это допустимо.
    await consents.revoke_active(user_id, CURRENT_POLICY_VERSION, now)

    # S3-удаление по каждому UGC-файлу гостя. Idempotency-ключ `forget:<id>`:
    # повтор confirm_forget после уже выполненного s3_delete не ставит задачу дважды.
    # Протокола UgcRepo нет — работаем с SqlUgcRepo через утиную типизацию.
    files = await ugc_repo.list_files_for_user(int(user_id))  # type: ignore[attr-defined]
    enqueued = 0
    for submission_id, bucket, key in files:
        try:
            await enqueue(
                jobs,
                "s3_delete",
                {"bucket": bucket, "key": key},
                run_after=now,
                idempotency_key=f"forget:{int(submission_id)}",
            )
            enqueued += 1
        except ConflictError:
            # Задача уже стоит с тем же ключом — не ошибка (повтор confirm_forget).
            pass

    # Hard delete строк UGC и FSM.
    ugc_deleted = await ugc_repo.delete_for_user(int(user_id))  # type: ignore[attr-defined]
    fsm_deleted = await tg_fsm_repo.delete_by_user(int(user_id))  # type: ignore[attr-defined]

    return ForgetResult(
        user_id=int(user_id),
        ugc_submissions_deleted=ugc_deleted,
        tg_fsm_rows_deleted=fsm_deleted,
        s3_delete_jobs_enqueued=enqueued,
    )
