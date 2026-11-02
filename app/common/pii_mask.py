from __future__ import annotations

from typing import Any, Final

# Правило 2 (00-правила-проекта.md): никогда не логируем и не возвращаем
# в ответах эти поля в открытом виде.
PII_KEYS: Final = frozenset(
    {
        "first_name",
        "last_name",
        "phone",
        "email",
        "telegram_username",
        "username",
    }
)

_MASK: Final = "***"


def mask_pii(obj: Any) -> Any:
    """Рекурсивно маскирует значения PII-ключей в dict/list.

    - dict: значения с ключами из PII_KEYS → "***" (ключ сам не меняем).
    - list/tuple: обходим элементы.
    - прочие типы (str/int/…): возвращаем без изменений.
    """
    if isinstance(obj, dict):
        return {
            k: _MASK if k in PII_KEYS else mask_pii(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_pii(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(mask_pii(x) for x in obj)
    return obj


def structlog_pii_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog-процессор: маскирует PII-поля в event_dict (включая вложенные)."""
    result = mask_pii(event_dict)
    assert isinstance(result, dict)
    return result
