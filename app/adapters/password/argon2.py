from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class Argon2PasswordHasher:
    """Argon2id через argon2-cffi с параметрами по умолчанию библиотеки.

    На 1-5 админов дефолтов (RFC 9106, профиль с низкой памятью:
    m=64 MiB, t=3, p=4) достаточно. Константа DUMMY_HASH в
    app/core/admin_access/service.py сгенерирована с теми же параметрами;
    это проверяет тест test_dummy_hash_matches_hasher_parameters.
    """

    def __init__(self) -> None:
        self._hasher = PasswordHasher()

    def hash(self, plain: str) -> str:
        return self._hasher.hash(plain)

    def verify(self, plain: str, hashed: str) -> bool:
        try:
            return self._hasher.verify(hashed, plain)
        except (VerificationError, InvalidHashError):
            # VerifyMismatchError - подкласс VerificationError; мусорный хэш
            # в БД тоже не должен ронять логин, а должен давать отказ.
            return False
