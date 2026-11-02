"""Реализация FileSnifferPort поверх python-magic (ADR-23, фаза 3b).

Одна и та же библиотека на всех платформах: python-magic-bin на Windows (libmagic внутри колёс)
и python-magic на Linux (нужна системная libmagic1, см. Dockerfile.app и CI). Вызывает адаптер
воркер фазы 3c; в фазе 3b его проверяет только тест.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Final

import magic

from app.core.ports.file_sniffer import MimeInfo

_FALLBACK_MIME: Final[str] = "application/octet-stream"


def _bundled_magic_database() -> str | None:
    """База сигнатур из колёс python-magic-bin (Windows) или None: тогда системная из libmagic1.

    libmagic на Windows открывает файлы узкими ANSI-вызовами: база по пути с кириллицей (папка
    OneDrive «Документы») не открывается, и загрузка падает с «could not find any valid magic
    files». Поэтому при не-ASCII пути базу копируем в ASCII-каталог кеша и используем её копию.
    """
    bundled = Path(magic.__file__).resolve().parent / "libmagic" / "magic.mgc"
    if not bundled.is_file():
        return None
    if str(bundled).isascii():
        return str(bundled)
    return str(_ascii_copy(bundled))


def _ascii_copy(source: Path) -> Path:
    """Копия базы в ASCII-каталоге (кеш LOCALAPPDATA или временный каталог). Запись атомарная."""
    for base in (os.environ.get("LOCALAPPDATA"), tempfile.gettempdir()):
        if not base or not base.isascii():
            continue
        target = Path(base) / "brand_activities_bot" / "magic" / "magic.mgc"
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            shutil.copyfile(source, staged)
            os.replace(staged, target)
        return target
    raise RuntimeError("magic_database_path_not_ascii")


class PythonMagicSniffer:
    """MIME по содержимому. Неизвестный формат даёт application/octet-stream, не исключение."""

    def __init__(self) -> None:
        # Дескриптор libmagic не потокобезопасен: один экземпляр на адаптер и замок вокруг вызова.
        self._magic = magic.Magic(mime=True, magic_file=_bundled_magic_database())
        self._lock = threading.Lock()

    def sniff(self, data: bytes) -> MimeInfo:
        with self._lock:
            mime = self._magic.from_buffer(data) or _FALLBACK_MIME
        return MimeInfo(mime=mime, is_image=mime.startswith("image/"))
