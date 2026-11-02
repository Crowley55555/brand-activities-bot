from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PasswordHasherPort(Protocol):
    """Хэширование паролей админки (ADR-8: Argon2id, реализация в адаптере).

    Методы синхронные и CPU-bound; вызывающий код ядра выносит их в поток
    (`asyncio.to_thread`), чтобы не блокировать event loop.
    """

    def hash(self, plain: str) -> str: ...

    def verify(self, plain: str, hashed: str) -> bool:
        """True, если пароль совпал; на любую ошибку верификации — False."""
        ...
