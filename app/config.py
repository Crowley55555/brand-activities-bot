from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@postgres:5432/brand_activities_bot",
        alias="DATABASE_URL",
    )
    internal_api_auth_secret: str = Field(
        default="", alias="INTERNAL_API_AUTH_SECRET"
    )
    internal_api_url: str = Field(
        default="http://app:8000", alias="INTERNAL_API_URL"
    )
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    # ADR-9: ключ подписи админ-сессии (itsdangerous). Пустой или оставшийся
    # плейсхолдером <...> из .env.example ключ считается «не настроен» —
    # /admin/* тогда отвечает 503 (fail-closed), см. app/admin/session.py.
    admin_session_secret: str = Field(default="", alias="ADMIN_SESSION_SECRET")
    # Secure-флаг cookie админки: true в проде (HTTPS), false в локальном HTTP.
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    # Фаза 3a: корень dev-хранилища UGC-файлов (adapter local_fs). В prod - S3 (фаза 3c).
    ugc_local_path: str = Field(default="./data/ugc", alias="UGC_LOCAL_PATH")
    # Фаза 3b (правило 5: TTL - в конфиге): срок ссылки на фото UGC (/media/<id>?t=), секунды.
    # 300 - «TTL ~5 минут» из канона 07 и 08 §4.3.3; верхняя граница 3600 - лимит адаптера.
    ugc_preview_ttl_seconds: int = Field(
        default=300, ge=1, le=3600, alias="UGC_PREVIEW_TTL_SECONDS"
    )
    # ТОП-3 ревью фазы 3c: visibility-таймаут блокировки по виду задачи, секунды.
    # ingest_ugc_file: httpx до 60 с + S3 upload -> 300 с; остальные (telegram_send) - 90 с.
    visibility_timeout_s_ingest: int = Field(
        default=300, ge=10, le=3600, alias="VISIBILITY_TIMEOUT_S_INGEST"
    )
    visibility_timeout_s_default: int = Field(
        default=90, ge=10, le=900, alias="VISIBILITY_TIMEOUT_S_DEFAULT"
    )
    # Фаза 3c (ADR-29): хранилище. local_fs - dev; selectel_s3 - Selectel и MinIO (через endpoint).
    object_storage_impl: Literal["local_fs", "selectel_s3"] = Field(
        default="local_fs", alias="OBJECT_STORAGE_IMPL"
    )
    s3_endpoint_url: str = Field(default="", alias="S3_ENDPOINT_URL")
    s3_access_key_id: str = Field(default="", alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(default="", alias="S3_SECRET_ACCESS_KEY")
    s3_region_name: str = Field(default="ru-1", alias="S3_REGION_NAME")
    s3_bucket_ugc: str = Field(default="ugc", alias="S3_BUCKET_UGC")
    # Фаза 3c (правило 5: тексты - в конфиге): шаблоны str.format, плейсхолдеры см. ADR-31.
    # ТОП-1 ревью фазы 3c: системный сбой приёмки (шаблон ingest_failed). Подпись причины
    # подставляется из activities.limits.system_reject_reasons (миграция 0011).
    outbox_text_ingest_failed_retry: str = Field(
        default="Ваш файл не удалось принять: {reason}. Попробуйте ещё раз через /start.",
        alias="OUTBOX_TEXT_INGEST_FAILED_RETRY",
    )
    outbox_text_ingest_failed_closed: str = Field(
        default="Технически приёмка завершилась неудачно, обратитесь к администратору.",
        alias="OUTBOX_TEXT_INGEST_FAILED_CLOSED",
    )
    outbox_text_reward_notice: str = Field(
        default="Готово! Промокод: {promocode}. Напиток: {voucher_title}. "
        "Код ваучера: {voucher_code}",
        alias="OUTBOX_TEXT_REWARD_NOTICE",
    )
    outbox_text_ugc_rejected_retry: str = Field(
        default="Ваша работа отклонена: {reason}. Пришлите другое фото через /start.",
        alias="OUTBOX_TEXT_UGC_REJECTED_RETRY",
    )
    outbox_text_ugc_rejected_closed: str = Field(
        default="Ваша работа отклонена: {reason}. К сожалению, попытки на сегодня исчерпаны.",
        alias="OUTBOX_TEXT_UGC_REJECTED_CLOSED",
    )

    # Фаза 4 (правило 5): тексты booking-уведомлений и публичный адрес для ссылки на листовку.
    outbox_text_appointment_reminder: str = Field(
        default="Напоминание: через {minutes} мин ваша запись «{activity}» в {time} по Москве.",
        alias="OUTBOX_TEXT_APPOINTMENT_REMINDER",
    )
    outbox_text_appointment_auto_cancelled: str = Field(
        default="Запись «{activity}» на {time} по Москве отменена: вы не отметились вовремя.",
        alias="OUTBOX_TEXT_APPOINTMENT_AUTO_CANCELLED",
    )
    outbox_text_appointment_leaflet: str = Field(
        default="Спасибо за визит! Листовка «{title}»: {url}",
        alias="OUTBOX_TEXT_APPOINTMENT_LEAFLET",
    )
    # Фаза 5 (правило 5): бакет результатов Polaroid (U9), TTL ссылки на результат (B4), тексты.
    s3_bucket_frames: str = Field(default="frames", alias="S3_BUCKET_FRAMES")
    media_frame_ttl_seconds: int = Field(
        default=600, ge=1, le=3600, alias="MEDIA_FRAME_TTL_SECONDS"
    )
    outbox_text_wheel_result: str = Field(
        default="Колесо момента: вам выпало «{prize}».",
        alias="OUTBOX_TEXT_WHEEL_RESULT",
    )
    outbox_text_polaroid_ready: str = Field(
        default="Ваш Polaroid готов!",
        alias="OUTBOX_TEXT_POLAROID_READY",
    )
    outbox_text_promocode: str = Field(
        default="{title}: ваш промокод {code}",
        alias="OUTBOX_TEXT_PROMOCODE",
    )
    outbox_text_confirmation_rejected: str = Field(
        default="Подарок за пост не выдан: {reason}.",
        alias="OUTBOX_TEXT_CONFIRMATION_REJECTED",
    )
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    # Ревью фазы 4 (M2): окружение для dev-скриптов. Пусто - скрипты демо-данных не работают.
    app_env: str = Field(default="", alias="APP_ENV")

    # Фаза 10b-1, C-1 (правило 5: TTL бизнес-правила - в конфиге, не в коде).
    # Канон 02 §7 и 04 §1.2: OTP-погашение именных наград живёт 5 минут.
    rewards_otp_ttl_minutes: int = Field(
        default=5, ge=1, le=60, alias="REWARDS_OTP_TTL_MINUTES"
    )

    # Фаза 10b-3, N-1 (правило 5): размер пачки scheduler-джобов expire_stale_attempts и
    # expire_pending_moderation. Хардкод LIMIT 500 из attempts_repo.py превращён в env.
    # Нижний предел 1 — чтобы тесты могли проверить батч-отсечение одной строки.
    expire_batch_size: int = Field(
        default=500, ge=1, le=10000, alias="EXPIRE_BATCH_SIZE"
    )

    # Фаза 10b-3, N-5 (правило 5): TTL протухших строк tg_fsm_state. Джоб cleanup_stale_fsm
    # раз в scheduler-тик удаляет строки с updated_at < now() - fsm_stale_hours.
    # Пилот: 24 часа — гость успевает закончить регистрацию, но брошенные диалоги не висят.
    fsm_stale_hours: int = Field(
        default=24, ge=1, le=720, alias="FSM_STALE_HOURS"
    )

    @field_validator("cookie_secure", mode="before")
    @classmethod
    def _empty_cookie_secure_means_false(cls, value: object) -> object:
        # `COOKIE_SECURE=` (пусто) в .env допустимо и означает «выключено».
        if isinstance(value, str) and not value.strip():
            return False
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
