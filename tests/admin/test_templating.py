"""Jinja2-рендер админки (ADR-21): автоэкранирование, строгие переменные, оболочка страниц."""

from __future__ import annotations

import pytest
from jinja2 import UndefinedError

from app.admin.templating import render, render_page
from app.core.types.dto import AdminRef
from app.core.types.ids import AdminId


def _body(resp: object) -> str:
    return bytes(resp.body).decode("utf-8")  # type: ignore[attr-defined]


def test_values_are_html_escaped_by_default() -> None:
    admin = AdminRef(id=AdminId(1), login="a@test", display_name='<img src=x onerror="alert(1)">')

    body = _body(render_page("index.html", title="Главная", admin=admin))

    assert "<img" not in body
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in body


def test_missing_context_value_fails_loudly() -> None:
    """StrictUndefined: нет rows для очереди - ошибка рендера, а не пустая таблица."""
    with pytest.raises(UndefinedError):
        render("moderation.html")


def test_template_name_cannot_traverse_directories() -> None:
    for evil in ("../templating.py", "..\\templating.py", "sub/dir.html"):
        with pytest.raises(ValueError):
            render(evil)


def test_render_page_wraps_content_with_base_and_nav() -> None:
    admin = AdminRef(id=AdminId(1), login="a@test", display_name="Анна")

    resp = render_page("index.html", title="Главная", admin=admin)

    body = _body(resp)
    assert resp.status_code == 200
    assert "<title>Главная — brand_activities_bot</title>" in body
    # I-3 фазы 10b-2: logout — форма POST с CSRF (канон 07 §4.4), а не GET-ссылка.
    assert 'action="/admin/logout"' in body
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-frame-options"] == "DENY"


def test_render_page_without_admin_has_no_nav() -> None:
    resp = render_page("login.html", title="Вход", csrf="c", next_url="/admin/", failed=False)

    assert "logout" not in _body(resp)


def test_login_error_is_shown_only_after_failed_attempt() -> None:
    failed = _body(
        render_page("login.html", title="Вход", csrf="c", next_url="", failed=True)
    )
    ok = _body(render_page("login.html", title="Вход", csrf="c", next_url="", failed=False))

    assert "неверный логин или пароль" in failed
    assert "неверный логин или пароль" not in ok
