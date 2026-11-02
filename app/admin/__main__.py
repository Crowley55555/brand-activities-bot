"""CLI админки: `python -m app.admin create_user`.

Создаёт сотрудника в admin_users (Argon2id-хэш через тот же PasswordHasherPort,
что и логин) БЕЗ привязок к брендам - права владелец выдаёт отдельно (MVP).
Пароль читается только интерактивно (getpass) и не принимается аргументом
командной строки, чтобы не оседать в истории шелла и списке процессов.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Sequence

from app.adapters.clock import build as build_clock
from app.adapters.password import build as build_password_hasher
from app.common.errors import ConflictError, ValidationError
from app.config import get_settings
from app.core.admin_access import create_admin
from app.core.types.dto import AdminRef
from app.db.engine import create_engine_from_url
from app.db.session import make_session_factory
from app.db.unit_of_work import UnitOfWork

_ERRORS_RU = {
    "login_invalid": "некорректный логин (пустой, с пробелами или длиннее 254 символов)",
    "display_name_invalid": "некорректное отображаемое имя (пустое или длиннее 200 символов)",
    "password_too_short": "пароль короче 10 символов",
    "password_too_long": "пароль длиннее 1024 символов",
    "admin_login_taken": "сотрудник с таким логином уже существует",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.admin",
        description="Управление сотрудниками админки brand_activities_bot.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser(
        "create_user",
        help="создать сотрудника админки (логин, пароль, отображаемое имя)",
    )
    create.add_argument("--login", help="логин; если не задан - спросим")
    create.add_argument(
        "--display-name", dest="display_name", help="имя в интерфейсе; если не задано - спросим"
    )
    return parser


async def _create_user(login: str, pw_in: str, display_name: str) -> AdminRef:
    engine = create_engine_from_url(get_settings().database_url)
    try:
        async with UnitOfWork(make_session_factory(engine)) as uow:
            return await create_admin(
                uow.repositories.admins,
                build_password_hasher(),
                login,
                pw_in,
                display_name,
                build_clock().now(),
            )
    finally:
        await engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = input,
    ask_pw: Callable[[str], str] = getpass.getpass,
) -> int:
    """Возвращает код выхода процесса: 0 - создан, 1 - отказ, 2 - ошибка вызова."""
    args = _build_parser().parse_args(argv)

    login = args.login if args.login is not None else prompt("Логин: ")
    display_name = (
        args.display_name if args.display_name is not None else prompt("Отображаемое имя: ")
    )
    first = ask_pw("Пароль: ")
    if first != ask_pw("Пароль ещё раз: "):
        print("Пароли не совпали, сотрудник не создан.", file=sys.stderr)
        return 1

    try:
        # SelectorEventLoop явно: psycopg async не работает на ProactorEventLoop,
        # который на Windows используется по умолчанию. loop_factory не трогает
        # глобальную policy процесса (3.12+).
        admin = asyncio.run(
            _create_user(login, first, display_name), loop_factory=asyncio.SelectorEventLoop
        )
    except (ValidationError, ConflictError) as exc:
        reason = _ERRORS_RU.get(exc.code, exc.code)
        print(f"Сотрудник не создан: {reason}.", file=sys.stderr)
        return 1

    print(f"Сотрудник создан, id={admin.id}. Привязок к брендам нет: права выдаёт владелец.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
