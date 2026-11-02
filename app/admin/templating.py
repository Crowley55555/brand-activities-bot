"""Рендер HTML админки на Jinja2 с автоэкранированием (ADR-21, фаза 3b).

Миграция со stdlib `string.Template` (ADR-11 фазы 1.5 закрыт): шаблоны на `{{ var }}`,
`{% extends %}` и `{% include %}`. Экранирование включено для html и xml. Небезопасный HTML
не передаём: `|safe` в шаблонах не используем. `StrictUndefined` делает опечатку в имени
переменной ошибкой, а не пустой строкой.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.responses import HTMLResponse

from app.core.types.dto import AdminRef

ADMIN_DIR: Final = Path(__file__).parent
TEMPLATES_DIR: Final = ADMIN_DIR / "templates"
STATIC_DIR: Final = ADMIN_DIR / "static"

_PAGE_HEADERS: Final = {
    # Страницы админки не кэшируем и не даём встраивать во фрейм (clickjacking).
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}


def _safe_url(value: str | None) -> str:
    """Ссылка для href: только https://. Иное (javascript:, data:, пусто) - пустая строка.

    Вторая линия защиты (замечание 5): на приёмке URL проверяет API (F3), но шаблон не должен верить
    данным, которые записали в базу в обход API.
    """
    if value and value.startswith("https://"):
        return value
    return ""


@cache
def _environment() -> Environment:
    # Путь - от файла модуля, а не от текущего каталога процесса (uvicorn, тесты, Docker).
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        auto_reload=False,
    )
    env.filters["safe_url"] = _safe_url
    return env


def render(template_name: str, **context: object) -> str:
    """Отрисовать шаблон в строку. Имя - только имя файла, без обхода каталогов."""
    if "/" in template_name or "\\" in template_name:
        raise ValueError("template name must be a bare file name")
    # admin по умолчанию None: base.html проверяет его для навигации (StrictUndefined).
    # csrf="" по умолчанию: nav.html держит форму POST /admin/logout (I-3), которой
    # нужен токен; страницы без своего CSRF (напр. index) показывают форму без
    # токена — POST тогда получит 403, и это ожидаемо (владелец откроет страницу
    # с CSRF и выйдет оттуда). Форма рисуется всегда, чтобы «Выход» всегда был.
    values: dict[str, object] = {"admin": None, "csrf": "", **context}
    return _environment().get_template(template_name).render(values)


def render_page(
    name: str,
    *,
    title: str,
    admin: AdminRef | None = None,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    body = render(name, title=title, admin=admin, **context)
    return HTMLResponse(body, status_code=status_code, headers=_PAGE_HEADERS)
