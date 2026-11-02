from __future__ import annotations

from typing import Final

# ADR-5 (2026-10-13): версия и хэш политики ПДн — константы.
# Пересчитываются одной и той же миграцией/PR при каждой правке текста
# политики: docs/policies/pdn-policy-v<N>.md → полный блок обновляется разом.
# Хэш считается командой:
#   py -3 -c "import hashlib; \
#   print(hashlib.sha256(open('docs/policies/pdn-policy-v1.md','rb').read()).hexdigest())"
CURRENT_POLICY_VERSION: Final[str] = "v1"
CURRENT_POLICY_TEXT_HASH: Final[str] = (
    "d40c0e4f070356b26c2b83019a479beeb9a534ad0002ec90e143ce45f9e79d9f"
)
