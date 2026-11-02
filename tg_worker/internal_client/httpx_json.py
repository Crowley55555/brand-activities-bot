from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("tg_worker.internal_client")


class FsmHttpError(RuntimeError):
    """FSM HTTP ошибка (фаза 10b-3, N-3): осмысленный тип вместо голого RuntimeError.

    aiogram-диспетчер ловит её по типу и gracefully деградирует:
    - fail-open на чтение (`fsm_get` → None/empty: следующий update затрёт);
    - fail-closed на запись (`fsm_set*`/`fsm_clear` пробрасывают дальше).

    Код формы `fsm_<op>_<status>`: `fsm_get_503`, `fsm_set_500` и т. п.
    Атрибуты `op`/`status` — для structured-логов на стороне storage.
    """

    def __init__(self, op: str, status: int) -> None:
        super().__init__(f"fsm_{op}_{status}")
        self.op = op
        self.status = status


class InternalApiClient:
    def __init__(self, base_url: str, auth_secret: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._hmac_bytes = auth_secret.encode("utf-8")
        # Один клиент на процесс: закрывается в aclose() при остановке (ревью, пункт I).
        self._client = httpx.AsyncClient(timeout=10.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._hmac_bytes, body, hashlib.sha256).hexdigest()

    async def _post(
        self, path: str, payload: dict[str, Any], *, retry: bool = True
    ) -> httpx.Response:
        """`retry=False` для шагов опроса: повтор после 5xx, который ядро уже выполнило,
        даёт ложный 409 (already_answered / already_closed / already_passed_today)."""
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Auth": self._sign(body),
        }
        url = f"{self._base_url}{path}"
        for attempt in (1, 2):
            try:
                resp = await self._client.post(url, content=body, headers=headers)
                if resp.status_code >= 500 and attempt == 1 and retry:
                    continue
                return resp
            except httpx.ConnectError:
                if attempt == 1:
                    continue
                raise
        raise RuntimeError("unreachable")

    async def _get(self, path: str) -> httpx.Response:
        # HMAC считается с пустым телом.
        headers = {"X-Internal-Auth": self._sign(b"")}
        url = f"{self._base_url}{path}"
        for attempt in (1, 2):
            try:
                resp = await self._client.get(url, headers=headers)
                if resp.status_code >= 500 and attempt == 1:
                    continue
                return resp
            except httpx.ConnectError:
                if attempt == 1:
                    continue
                raise
        raise RuntimeError("unreachable")

    async def send_update(
        self, raw_update: dict[str, Any], received_at: datetime
    ) -> dict[str, Any]:
        payload = {"update": raw_update, "received_at": received_at.isoformat()}
        resp = await self._post(
            "/internal/v1/channels/telegram/updates", payload
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def accept_consent(
        self, channel_type: str, external_id: str
    ) -> dict[str, Any]:
        resp = await self._post(
            "/internal/v1/consent/accept",
            {"channel_type": channel_type, "external_id": external_id},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def update_pii(
        self,
        channel_type: str,
        external_id: str,
        first_name: str,
        last_name: str,
        phone: str,
        email: str,
        policy_version: str,
    ) -> tuple[int, dict[str, Any]]:
        resp = await self._post(
            "/internal/v1/users/pii/update",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "email": email,
                "policy_version": policy_version,
            },
        )
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError:
            payload = {}
        return resp.status_code, payload

    async def list_brands(self) -> list[dict[str, Any]]:
        resp = await self._get("/internal/v1/catalog/brands")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        brands = data.get("brands") or []
        assert isinstance(brands, list)
        return brands

    async def list_brand_activities(self, brand_slug: str) -> list[dict[str, Any]]:
        """Активности бренда из ядра (slug и kind берём отсюда, а не из кода). Нет бренда -> []."""
        slug = quote(brand_slug, safe="")
        resp = await self._get(f"/internal/v1/catalog/brands/{slug}/activities")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        activities = data.get("activities") or []
        assert isinstance(activities, list)
        return activities

    async def start_activity(
        self, channel_type: str, external_id: str, activity_slug: str
    ) -> tuple[int, dict[str, Any]]:
        resp = await self._post(
            "/internal/v1/activities/start",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "activity_slug": activity_slug,
            },
            retry=False,
        )
        return _status_and_body(resp)

    async def booking_list_slots(
        self, channel_type: str, external_id: str, activity_slug: str
    ) -> tuple[int, dict[str, Any]]:
        """Свободные слоты на сегодня. Чтение - повтор безопасен (retry по умолчанию)."""
        resp = await self._post(
            "/internal/v1/activities/booking/list_slots",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "activity_slug": activity_slug,
            },
        )
        return _status_and_body(resp)

    async def booking_book(
        self, channel_type: str, external_id: str, activity_slug: str, slot_id: int
    ) -> tuple[int, dict[str, Any]]:
        """Бронирование. retry=False: повтор после 5xx дал бы ложный already_passed_today."""
        resp = await self._post(
            "/internal/v1/activities/booking/book",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "activity_slug": activity_slug,
                "slot_id": slot_id,
            },
            retry=False,
        )
        return _status_and_body(resp)

    async def wheel_spin(
        self, channel_type: str, external_id: str, activity_slug: str
    ) -> tuple[int, dict[str, Any]]:
        """Спин колеса. retry=False: повтор после 5xx дал бы ложный already_spun_today."""
        resp = await self._post(
            "/internal/v1/activities/wheel/spin",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "activity_slug": activity_slug,
            },
            retry=False,
        )
        return _status_and_body(resp)

    async def photo_frame_accept(
        self,
        channel_type: str,
        external_id: str,
        activity_slug: str,
        tg_file_id: str,
        mime_type: str,
        file_size_bytes: int,
    ) -> tuple[int, dict[str, Any]]:
        """Фото для рамки. retry=False: повтор после 5xx дал бы ложный already_passed_today."""
        resp = await self._post(
            "/internal/v1/activities/photo_frame/accept",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "activity_slug": activity_slug,
                "tg_file_id": tg_file_id,
                "mime_type": mime_type,
                "file_size_bytes": file_size_bytes,
            },
            retry=False,
        )
        return _status_and_body(resp)

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
        """Фото UGC и ссылки. retry=False: повтор после 5xx дал бы ложный 409 (не в работе)."""
        resp = await self._post(
            "/internal/v1/activities/ugc/accept",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "attempt_id": attempt_id,
                "tg_file_id": tg_file_id,
                "mime_type": mime_type,
                "file_size_bytes": file_size_bytes,
                "post_url": post_url,
                "subscription_url": subscription_url,
            },
            retry=False,
        )
        return _status_and_body(resp)

    async def confirmation_accept(
        self, channel_type: str, external_id: str, activity_slug: str, post_url: str
    ) -> tuple[int, dict[str, Any]]:
        """Ссылка на пост. retry=False: повтор после 5xx дал бы ложный already_passed_today."""
        resp = await self._post(
            "/internal/v1/activities/confirmation/accept",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "activity_slug": activity_slug,
                "post_url": post_url,
            },
            retry=False,
        )
        return _status_and_body(resp)

    async def answer_activity(
        self,
        channel_type: str,
        external_id: str,
        attempt_id: int,
        question_id: int,
        selected_option_keys: list[str],
        free_text: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        resp = await self._post(
            "/internal/v1/activities/answer",
            {
                "channel_type": channel_type,
                "external_id": external_id,
                "attempt_id": attempt_id,
                "question_id": question_id,
                "selected_option_keys": selected_option_keys,
                "free_text": free_text,
            },
            retry=False,
        )
        return _status_and_body(resp)

    async def finish_activity(
        self, channel_type: str, external_id: str, attempt_id: int
    ) -> tuple[int, dict[str, Any]]:
        resp = await self._post(
            "/internal/v1/activities/finish",
            {"channel_type": channel_type, "external_id": external_id, "attempt_id": attempt_id},
            retry=False,
        )
        return _status_and_body(resp)

    async def outbox_claim(
        self, worker_id: str, limit: int, visibility_timeout_s: int
    ) -> list[dict[str, Any]]:
        """ADR-32: забрать пачку telegram_send; ядро отдаёт готовый text (п.8 ревью)."""
        resp = await self._post(
            "/internal/v1/channels/telegram/outbox/claim",
            {
                "worker_id": worker_id,
                "limit": limit,
                "visibility_timeout_s": visibility_timeout_s,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"outbox_claim_failed:{resp.status_code}")
        return [dict(item) for item in resp.json().get("jobs", [])]

    async def outbox_ack(
        self,
        job_id: int,
        worker_id: str,
        result: str,
        error: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        """ADR-32: исход отправки. Тело - job_id, worker_id (fencing, ТОП-3), result, error?."""
        payload: dict[str, Any] = {"job_id": job_id, "worker_id": worker_id, "result": result}
        if error is not None:
            payload["error"] = error
        if retry_after_s is not None:
            payload["retry_after_s"] = retry_after_s
        resp = await self._post("/internal/v1/channels/telegram/outbox/ack", payload)
        if resp.status_code != 200:
            raise RuntimeError(f"outbox_ack_failed:{resp.status_code}")

    # ===== Rewards OTP (C-1, фаза 10b-1) =====
    # tg_worker показывает «мои награды» и просит OTP на кнопку «подойду сейчас».
    # retry=False на request_otp: повтор после 5xx перепишет OTP и покажет чужой код.

    async def list_active_rewards(
        self, channel_type: str, external_id: str
    ) -> list[dict[str, Any]]:
        """Активные именные награды гостя (voucher, physical_gift) для `/my`."""
        resp = await self._post(
            "/internal/v1/rewards/list_active",
            {"channel_type": channel_type, "external_id": external_id},
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        rewards = data.get("rewards") or []
        assert isinstance(rewards, list)
        return rewards

    async def request_reward_otp(
        self, channel_type: str, external_id: str, reward_id: int
    ) -> tuple[int, dict[str, Any]]:
        """OTP на именную награду (канон 04 §1.2). retry=False: ядро пишет OTP на награду."""
        resp = await self._post(
            f"/internal/v1/rewards/{reward_id}/request_otp",
            {"channel_type": channel_type, "external_id": external_id},
            retry=False,
        )
        return _status_and_body(resp)

    # ===== FSM storage (C-4, фаза 10a; N-2/N-3 фазы 10b-3) =====
    # tg_worker ходит в ядро за FSM-состоянием: правило 4 (внешний сервис через порт).
    # Повторы допустимы: get/set/clear идемпотентны по контракту ядра.
    # N-2 (фаза 10b-3): для set используются /set_state_only и /set_data_only —
    # атомарный UPSERT без read-modify-write в tg_worker.
    # N-3 (фаза 10b-3): не-200 вместо голого RuntimeError бросает FsmHttpError
    # с warn-логом (storage ловит по типу).

    def _raise_fsm(self, op: str, status: int) -> None:
        """Warn-лог + FsmHttpError (N-3 фазы 10b-3)."""
        logger.warning(
            "fsm_http_failed",
            extra={"event": "fsm_http_failed", "op": op, "status": status},
        )
        raise FsmHttpError(op, status)

    async def fsm_get(self, key: str) -> tuple[str | None, dict[str, Any]]:
        """Получить состояние и данные по ключу. Нет записи -> (None, {})."""
        resp = await self._post(
            "/internal/v1/channels/tg/fsm/get", {"key": key}
        )
        if resp.status_code != 200:
            self._raise_fsm("get", resp.status_code)
        data: dict[str, Any] = resp.json()
        state = data.get("state")
        payload = data.get("data") or {}
        return (None if state is None else str(state), dict(payload))

    async def fsm_set_state(
        self, key: str, state: str | None, user_id: int | None = None
    ) -> None:
        """Атомарный UPSERT: только state, data не трогается (N-2 фазы 10b-3)."""
        payload: dict[str, Any] = {"key": key, "state": state}
        if user_id is not None:
            payload["user_id"] = int(user_id)
        resp = await self._post(
            "/internal/v1/channels/tg/fsm/set_state_only", payload
        )
        if resp.status_code != 200:
            self._raise_fsm("set_state", resp.status_code)

    async def fsm_set_data(
        self, key: str, data: dict[str, Any], user_id: int | None = None
    ) -> None:
        """Атомарный UPSERT: только data, state не трогается (N-2 фазы 10b-3)."""
        payload: dict[str, Any] = {"key": key, "data": data}
        if user_id is not None:
            payload["user_id"] = int(user_id)
        resp = await self._post(
            "/internal/v1/channels/tg/fsm/set_data_only", payload
        )
        if resp.status_code != 200:
            self._raise_fsm("set_data", resp.status_code)

    async def fsm_clear(self, key: str) -> None:
        """Удалить запись по ключу (повтор idempotent)."""
        resp = await self._post(
            "/internal/v1/channels/tg/fsm/clear", {"key": key}
        )
        if resp.status_code != 200:
            self._raise_fsm("clear", resp.status_code)

    # ===== /forget (фаза 10b-3, I-2) =====
    # Канон 08 §335: tg-handler зовёт ядро под HMAC. Ответ 200 OK — анонимизация
    # сделана, все UGC-файлы в очереди на удаление, FSM-строки пользователя снесены.

    async def forget_user(
        self, channel_type: str, external_id: str
    ) -> tuple[int, dict[str, Any]]:
        """`/forget` каскад (анонимизация ПДн + hard delete UGC и tg_fsm_state).

        `retry=False`: повтор после 5xx мог бы попасть в уже анонимизированного
        пользователя (ядро отвечает 409 already_anonymized), что хэндлер
        неправильно интерпретировал бы.
        """
        resp = await self._post(
            "/internal/v1/users/forget",
            {"channel_type": channel_type, "external_id": external_id},
            retry=False,
        )
        return _status_and_body(resp)


def _status_and_body(resp: httpx.Response) -> tuple[int, dict[str, Any]]:
    """Статус и JSON-тело ответа. Тело не в JSON -> пустой dict: хендлер покажет общий текст."""
    try:
        payload: Any = resp.json()
    except ValueError:
        payload = {}
    return resp.status_code, payload if isinstance(payload, dict) else {}


async def send_update(
    client: InternalApiClient,
    raw_update: dict[str, Any],
    received_at: datetime,
) -> dict[str, Any]:
    return await client.send_update(raw_update, received_at)
