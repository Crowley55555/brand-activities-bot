from __future__ import annotations

from typing import Final

# Хэш случайного пароля, который нигде не хранится и не печатался. Нужен для
# выравнивания времени логина: когда логина нет / админ неактивен, core всё
# равно тратит одну верификацию (core.admin_access.authenticate), и по времени
# ответа нельзя понять, существует ли логин (user enumeration).
#
# Живёт в common, а не в core: формат хэша - знание конкретного алгоритма, а
# ядро алгоритма не знает (правило 4). Параметры (m=65536,t=3,p=4) обязаны
# совпадать с параметрами адаптера app/adapters/password; это проверяет
# tests/core/test_admin_access_service.py::test_dummy_hash_matches_hasher_parameters.
# Перегенерация: PasswordHasher().hash(secrets.token_urlsafe(48)) с теми же
# параметрами; сам пароль не сохранять.
DUMMY_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=4$UKwEtv7V6QlMfKsM/+zJWg$"
    "Sj4eYhceEMj4XJdWV2lOEZS/UDPoimSg6ZgifXNhQAE"
)
