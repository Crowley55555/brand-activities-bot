from __future__ import annotations

from app.adapters.password.argon2 import Argon2PasswordHasher
from app.core.ports.password_hasher import PasswordHasherPort


def build() -> PasswordHasherPort:
    return Argon2PasswordHasher()


__all__ = ["Argon2PasswordHasher", "build"]
