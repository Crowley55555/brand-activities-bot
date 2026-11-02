"""Адаптер Argon2id (ADR-8): формат хэша, соль, устойчивость verify."""

from __future__ import annotations

from app.adapters.password import Argon2PasswordHasher, build
from app.core.ports.password_hasher import PasswordHasherPort


def test_build_returns_an_object_satisfying_the_port() -> None:
    hasher = build()

    assert isinstance(hasher, PasswordHasherPort)
    assert isinstance(hasher, Argon2PasswordHasher)


def test_hash_is_argon2id_and_salted() -> None:
    hasher = build()

    first = hasher.hash("same-input")
    second = hasher.hash("same-input")

    assert first.startswith("$argon2id$")
    assert first != second, "соль случайна: два хэша одного пароля различаются"
    assert hasher.verify("same-input", first) and hasher.verify("same-input", second)


def test_verify_never_raises_on_garbage_hashes() -> None:
    hasher = build()

    for garbage in ("", "plain-text", "$argon2id$broken", "$2b$12$notargon", "\x00"):
        assert hasher.verify("anything", garbage) is False


def test_verify_is_case_and_whitespace_sensitive() -> None:
    hasher = build()
    stored = hasher.hash("Secret Phrase")

    assert hasher.verify("Secret Phrase", stored) is True
    assert hasher.verify("secret phrase", stored) is False
    assert hasher.verify("Secret Phrase ", stored) is False
    assert hasher.verify("", stored) is False
