"""Публичная дверь Polaroid-приёмки (08 §3): снаружи ходят только через этот пакет."""

from __future__ import annotations

from app.core.activities.photo_frame.repository import FrameRepo
from app.core.activities.photo_frame.service import (
    CANCEL_REASON,
    RENDER_FAILED_SLUG,
    accept_source,
    check_declared_file,
    mark_failed,
    mark_ingest_failed,
    mark_result,
    mark_source_stored,
)
from app.core.activities.photo_frame.types import FrameAcceptResult, FrameSubmissionRef

__all__ = [
    "CANCEL_REASON",
    "RENDER_FAILED_SLUG",
    "FrameAcceptResult",
    "FrameRepo",
    "FrameSubmissionRef",
    "accept_source",
    "check_declared_file",
    "mark_failed",
    "mark_ingest_failed",
    "mark_result",
    "mark_source_stored",
]
