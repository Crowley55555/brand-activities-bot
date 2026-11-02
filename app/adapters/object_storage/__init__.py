from __future__ import annotations

from pathlib import Path

from app.adapters.object_storage.local_fs import LocalFsObjectStorage
from app.adapters.object_storage.selectel_s3 import build_selectel_s3
from app.config import Settings
from app.core.ports.object_storage import ObjectStoragePort


def build(settings: Settings) -> ObjectStoragePort:
    """Выбор реализации по OBJECT_STORAGE_IMPL (ADR-29): local_fs (dev) или selectel_s3 (prod)."""
    if settings.object_storage_impl == "selectel_s3":
        return build_selectel_s3(settings)
    return LocalFsObjectStorage(
        root=Path(settings.ugc_local_path),
        signing_key=settings.internal_api_auth_secret,
    )


__all__ = ["build"]
