"""Типы Polaroid-приёмки (ADR-40): результат приёмки и строка frame_submissions."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.types.ids import AttemptId


@dataclass(frozen=True)
class FrameAcceptResult:
    submission_id: int
    attempt_id: AttemptId
    status: str


@dataclass(frozen=True)
class FrameSubmissionRef:
    """Строка frame_submissions. Исходник и результат - ссылки на объектное хранилище, не байты."""

    id: int
    attempt_id: AttemptId
    status: str
    source_bucket: str | None
    source_key: str | None
    source_content_type: str | None
    source_size_bytes: int | None
    result_bucket: str | None
    result_key: str | None
    result_content_type: str | None
    result_size_bytes: int | None
    failure_reason: str | None
