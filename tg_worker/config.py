from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    internal_api_url: str = Field(
        default="http://app:8000", alias="INTERNAL_API_URL"
    )
    internal_api_auth_secret: str = Field(
        default="", alias="INTERNAL_API_AUTH_SECRET"
    )
    # ТОП-3: идентификатор воркера для claim/ack (fencing). Пусто - hostname:pid.
    outbox_worker_id: str = Field(default="", alias="OUTBOX_WORKER_ID")


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
