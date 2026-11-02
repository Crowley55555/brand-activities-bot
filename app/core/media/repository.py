from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.media.types import SubmissionFileRef
from app.core.types.ids import UgcSubmissionId


@runtime_checkable
class MediaRepo(Protocol):
    async def get_submission_by_id(
        self, submission_id: UgcSubmissionId
    ) -> SubmissionFileRef | None:
        """Подача с файлом в хранилище. Нет строки или файла ещё нет - None."""
        ...
