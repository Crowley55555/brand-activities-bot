"""CLI `python -m app.admin create_user` (критерий приёмки 5): реальная БД, без argv-паролей."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import Row, text
from sqlalchemy.engine import Engine

from tests.admin.conftest import ARGON_PLAIN, fetch_csrf, find_set_cookie, login_form


def _scripted(answers: list[str]) -> Callable[[str], str]:
    queue = iter(answers)

    def _ask(_prompt: str) -> str:
        return next(queue)  # StopIteration = CLI спросил то, чего спрашивать не должен

    return _ask


def _run(argv: list[str], *, prompts: list[str], hidden: list[str]) -> int:
    from app.admin.__main__ import main

    return main(argv, prompt=_scripted(prompts), ask_pw=_scripted(hidden))


def _rows(engine: Engine, login: str) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, display_name, password_hash, is_active, "
                    "deleted_at, last_login_at FROM admin_users WHERE login = :login"
                ),
                {"login": login},
            )
        )


def test_create_user_inserts_admin_with_argon2id_hash(
    sync_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.adapters.password import build

    code = _run(
        ["create_user"], prompts=["boss@test", "Босс"], hidden=[ARGON_PLAIN, ARGON_PLAIN]
    )

    assert code == 0
    (row,) = _rows(sync_engine, "boss@test")
    assert row.display_name == "Босс"
    assert row.is_active is True
    assert row.deleted_at is None and row.last_login_at is None
    stored = row.password_hash
    assert stored.startswith("$argon2id$") and ARGON_PLAIN not in stored
    assert build().verify(ARGON_PLAIN, stored)
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM admin_brands")).scalar_one() == 0
    shown = capsys.readouterr()
    assert "id=" in shown.out
    assert ARGON_PLAIN not in shown.out + shown.err, "пароль не должен печататься"


def test_login_and_display_name_flags_skip_the_prompts(sync_engine: Engine) -> None:
    code = _run(
        ["create_user", "--login", "flag@test", "--display-name", "Флаг"],
        prompts=[],
        hidden=[ARGON_PLAIN, ARGON_PLAIN],
    )

    assert code == 0
    assert len(_rows(sync_engine, "flag@test")) == 1


def test_password_mismatch_creates_nothing(
    sync_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(
        ["create_user"], prompts=["typo@test", "Опечатка"], hidden=[ARGON_PLAIN, ARGON_PLAIN + "x"]
    )

    assert code == 1
    assert _rows(sync_engine, "typo@test") == []
    assert "не совпали" in capsys.readouterr().err


def test_duplicate_login_is_refused_and_keeps_the_first_account(
    sync_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _run(["create_user"], prompts=["dup@test", "Первый"], hidden=[ARGON_PLAIN] * 2)
    second = _run(
        ["create_user"], prompts=["dup@test", "Второй"], hidden=["another-long-phrase"] * 2
    )

    assert (first, second) == (0, 1)
    (row,) = _rows(sync_engine, "dup@test")
    assert row.display_name == "Первый"
    assert "уже существует" in capsys.readouterr().err


def test_short_password_is_refused(sync_engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run(["create_user"], prompts=["weak@test", "Слабый"], hidden=["short"] * 2)

    assert code == 1
    assert _rows(sync_engine, "weak@test") == []
    assert "короче 10" in capsys.readouterr().err


def test_a_subcommand_is_required() -> None:
    from app.admin.__main__ import main

    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2


async def test_cli_created_admin_can_log_in_over_http(admin_client: AsyncClient) -> None:
    """Сквозной контроль: тот же хэшер и та же таблица, что использует логин."""
    code = await asyncio.to_thread(
        _run, ["create_user"], prompts=["cli@test", "Из CLI"], hidden=[ARGON_PLAIN] * 2
    )
    assert code == 0
    csrf = await fetch_csrf(admin_client)

    resp = await admin_client.post(
        "/admin/login", data=login_form("cli@test", ARGON_PLAIN, csrf)
    )

    assert resp.status_code == 302 and resp.headers["location"] == "/admin/"
    assert find_set_cookie(resp, "admin_session") is not None
