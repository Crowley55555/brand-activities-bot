# brand_activities_bot

Telegram-чат-бот активностей для пяти бьюти-брендов на headless-ядре FastAPI. Один блок регистрации с согласием на ПДн по 152-ФЗ, пять брендовых активностей, UGC-модерация, выдача промокодов/ваучеров, запись на слоты, напоминания, колесо призов и Polaroid-рамка для фото. Мессенджер — тонкий адаптер (aiogram), заменимый на MAX/WhatsApp/веб без правки бизнес-логики.

**Статус:** MVP (фазы 0..9 закрыты, 835 pytest passed, задеплоен на 2 сервера — RU + NL). Образец/портфолио, не оплачиваемый заказ.

---

## 1. Что реализовано

### Регистрация и согласие (фаза 1)

- `/start` → показ политики ПДн v1 → кнопка «Принять» → FSM «имя → фамилия → телефон → email» → меню из 5 брендов.
- `/my` — личные промокоды и ваучеры.
- `/forget` — двойное подтверждение → анонимизация (не физическое удаление; промокоды архивируются).
- Нормализация телефона в E.164, валидация email.
- PII-masking в логах (middleware + structlog processor) — ни одна строка `logger.info/warning/error` не пишет сырые ПДн.

### 5 брендов × 10 активностей

| Бренд | Активность | Движок | Что получает гость |
|---|---|---|---|
| **Бренд A** | «Код кожи» | survey_with_recommendations | карточки продуктов + промокод BRAND_A-XXX |
| Бренд A | диагностика (запись к специалисту) | booking | слот + подтверждение |
| **Бренд B** | UGC-фото + ссылка на пост + подписка | ugc_photo_link_subscription | ваучер «напиток» + промокод BRAND_B-XXX (после модерации) |
| **Бренд C** | запись к стилисту | booking_with_reminders | слот + напоминание за 5 мин + авто-отмена через 5 мин no-show + листовка PDF при mark_arrived |
| **Бренд D** | колесо призов | wheel (взвешенный random) | 1 из 5 призов (с лимитом остатков), 1 спин/день |
| Бренд D | Polaroid-фото | photo_frame | фото с рамкой через PIL |
| Бренд D | подарок за пост | confirmation (UGC-link без файла) | промокод BRAND_D-XXX после одобрения ссылки |
| **Бренд E** | «Код настроения» | survey_with_recommendations | аромат-подборка + промокод BRAND_E-XXX |
| Бренд E | фото аромата | ugc (без ваучера) | промокод BRAND_E-XXX после модерации |

### Админка (`/admin/*`, фазы 1.5, 3b, 4, 7a-d)

- `/admin/login` — Argon2id + session cookie (signed, HTTPOnly, Secure, SameSite=Lax, TTL 8ч) + CSRF double-submit.
- `/admin/ugc` — очередь модерации с disposition `approve/reject`, диспетчер по kind (ugc / confirmation), per-brand RBAC через `admin_brands`.
- `/admin/schedule` — слоты (single и bulk до 100 за раз), «Пришёл»/«Не пришёл», аудит ошибок валидации с человекочитаемыми ярлыками.
- `/admin/users` — список с маской ПДн (`А*** +79***4567 a***@g***.com`), кнопка «Показать телефон» → запись в `audit_log` с `action='reveal_user_phone'`.
- `/admin/rewards` — промокоды и ваучеры с фильтрами по типу / статусу / активности, `user_id` (не ПДн) в выдаче.
- `/admin/metrics?days=N` — регистрации / completed+approved прохождения / промокоды / ваучеры по дням МСК; фильтр по бренду.

### Внутренний API и воркеры

- `/internal/v1/*` — HMAC-подписанные вызовы между tg_worker и app (`verify_internal_hmac`).
- `/media/<id>?t=<token>` — отдача UGC-файлов по подписи itsdangerous (без БД-токенов).
- Jobs worker (`app/workers/`) — SKIP LOCKED claim, backoff `min(60·2^(a−1), 2h) ±20%`, fencing по `locked_by`, idempotency_key по бизнес-ключу.
- Шаблоны outbox: `reward_notice`, `promocode`, `ugc_rejected`, `appointment_leaflet`, `wheel_result`, `polaroid_ready`, `confirmation_rejected` — тексты в конфиге (`OUTBOX_TEXT_*`), коды и ссылки формируются в `claim_outbox`.

---

## 2. Архитектурные решения («почему именно так»)

### Headless-ядро, hexagonal, порты и адаптеры

Весь домен (регистрация, активности, выдача наград, модерация, колесо, слоты, scheduler) живёт в `app/core/*`. Мессенджеры — тонкие адаптеры в `app/adapters/telegram/` (парсинг входа и рендер ответа). Любой внешний ресурс поднимается через **порт** в `app/core/ports/*` и реализацию в `app/adapters/<name>/`:

| Порт | Default-адаптер | Зачем |
|---|---|---|
| `TelegramFilesPort` | httpx + api.telegram.org | скачивание user-фото в ingest-джобе |
| `ObjectStoragePort` | `local_fs` (dev) / `selectel_s3` (prod) | хранение UGC |
| `ClockPort` | system clock / freezegun в тестах | детерминизм freezegun-тестов на booking |
| `RandomPort` | `secrets.SystemRandom` / `SeededRandomPort` в тестах | взвешенный выбор призов колеса |
| `PasswordHasherPort` | `Argon2PasswordHasher` | смена хэшера без правки core |
| `FileSnifferPort` | `python-magic` | валидация MIME по байтам (не только по декларации) |
| `ImageFramePort` | `PillowImageFrame` | Polaroid-рамка, можно заменить на ImageMagick |

**Следствие:** добавление MAX/WhatsApp/веб-канала = новая папка в `app/adapters/`, без правки `app/core/*`. Это проверено архитектурно (grep `from aiogram|import aiogram|boto3|httpx\.(get|post|AsyncClient)` в `app/core/` даёт пусто).

### Бизнес-специфика в БД, не в коде

Слоги брендов, вопросы опросов, URL карточек продуктов, цены, веса призов колеса, шаблоны промокодов, TTL, причины отказа модератора — всё в seed-миграциях (`alembic/versions/*_seed_*.py`) или в `activities.limits` JSONB. В коде `app/core/*` литералов «Бренд A», «Бренд B», «Бренд E» и т.п. — ровно 0 (кроме тестов и data-файлов миграций). Проверяется grep'ом `"\"Бренд A\""` и аналогов.

**Следствие:** правка текстов, цен, вопросов — одной миграцией без деплоя кода. Новый бренд — одна миграция.

### Consent-gate first-class (правило 1)

Запись ПДн (`first_name`, `last_name`, `phone`, `email`, `telegram_username`) происходит **только** после валидного `Consent(user_id, policy_version=CURRENT, revoked_at IS NULL)`. Каждый use-case, пишущий ПДн, первой строкой зовёт `require_consent`. Без согласия — 409 и ничего не записывается. Проверяется приёмочным тестом `tests/api/test_consent.py::test_register_without_consent_rejects_409`.

**Следствие:** 152-ФЗ-соответствие по дизайну, не по контролю; невозможно случайно записать ПДн.

### PII-masking в логах (правило 2)

- `structlog_pii_processor` в `app/common/pii_mask.py` маскирует `phone`, `email`, `first_name`, `last_name`, `telegram_username` в структурированных полях.
- `pii_mask` middleware делает то же для тел запросов/ответов.
- В `app/core/activities/ugc/__init__.py` комментарий: «*Ссылки и tg_file_id в лог не пишем (правило 2): только факт приёма и внутренние id.*»
- Caddy access-log на prod **выключен** — URL `/media/<id>?t=<token>` содержат токен, который не должен попадать в журналы доступа.

**Следствие:** ПДн не утекают в stdout, Loki, Sentry, access-log. Проверяется `tests/logging/test_pii_masking.py`.

### Journal of decisions (46 ADR, правило 6)

Каждое архитектурное решение — строка `ДАТА — формулировка — источник` в журнале соответствующего файла `architecture/00`...`10`. 46 ADR от 2026-10-11 до 2026-10-18 покрывают: Argon2id, Jinja2, CSRF app-level, SKIP LOCKED jobs, bool-ноты в Бренд E-опросе, ветвление approve по `_has_voucher`, флаги `requires_post_url/subscription_url` в `activities.limits`, `ActivityOut.limits` для tg-handlers, и т.д.

**Следствие:** любое архитектурное изменение трассируется до бизнес-требования; новые разработчики находят «почему именно так» за один grep.

### Idempotent jobs + outbox pattern

- Все jobs (`ingest_ugc_file`, `notify_reward`, `notify_promocode`, `notify_ugc_rejected`, `polaroid_render`, `telegram_send`, `auto_cancel_appointment`) создаются с `idempotency_key` по бизнес-ключу (например, `notify_reward:<submission_id>`).
- Worker claim через `SELECT ... FOR UPDATE SKIP LOCKED` + fencing по `locked_by` — безопасный горизонтальный scale.
- Outbox-таблица (jobs с kind `telegram_send`) → tg_worker long-polling claim'ит и отправляет гостю. Между core и tg нет shared-state кроме БД.

**Следствие:** одна награда — одно сообщение гостю (не два); воркер падает/перезапускается без потери задач; добавление второго воркера не требует изменений.

### APScheduler persistent + reconciliation

Напоминания и авто-отмена записей (Бренд C) — через APScheduler с `SQLAlchemyJobStore`. При рестарте `app` запускается `on_startup`-реконсиляция: для каждой `appointment` в статусе `booked/reminded/arrived` восстанавливается соответствующий job. **Проверяется freezegun-тестом**.

**Следствие:** рестарт prod-а не ломает напоминания.

### Signed URLs для медиа (без БД-токенов)

`/media/<submission_id>?t=<token>` — токен подписывается itsdangerous от `INTERNAL_API_AUTH_SECRET` c солью `ugc-media-token-v1`. Валидация без обращения к БД, TTL в самом токене.

**Следствие:** нет таблицы «media-токенов», не нужен VACUUM, нельзя подделать без секрета.

---

## 3. Безопасность и ПДн

| Что | Как |
|---|---|
| Пароли админов | Argon2id (`PasswordHasherPort` + argon2-cffi), timing-constant вход |
| Сессия админа | HttpOnly + Secure + SameSite=Lax, signed itsdangerous (HMAC-SHA256), TTL 8ч, перепроверка `is_active`/`deleted_at` на каждом запросе |
| CSRF | double-submit app-level `csrf_guard`, исключения: безопасные методы, `/internal/v1/*` (HMAC), `/health`, `/admin/static/*` |
| ПДн в репо | `.env`, `.env.*` кроме `.env.example`, `*.key`, `*.pem`, `/data/`, `/uploads/`, `/backups/`, медиа — в `.gitignore` + локальный pre-commit хук сканирует секреты (fail-closed) |
| ПДн в сторонние сервисы | в Telegram API уходят только `chat_id` + тексты + ссылки на `/media/<id>` нашего сервера |
| Внешние вызовы | HMAC-подписанные `/internal/v1/*`, `WEBHOOK_SECRET` на будущий webhook |
| MIME валидация | python-magic (байты), не только декларация клиента |
| Загрузка файла | ≤ 5 МБ, whitelist MIME (jpeg/png/webp) в `activities.limits.allowed_mimes` и `max_file_bytes` |
| Audit log | `audit_log` таблица с CHECK `object_type IN (users, consents, channel_accounts, attempts, rewards, ugc_submissions, appointments, wheel_spins, frame_submissions)` |

---

## 4. Стек и структура кода

**Стек:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), psycopg[binary] 3, Pydantic 2, Alembic, aiogram 3.x, APScheduler, Pillow, python-magic, aioboto3, structlog, itsdangerous, Argon2-cffi, Jinja2. Пакетный менеджер — `uv` (`uv.lock` в git).

```
app/
├── core/              # headless-ядро, бизнес-логика
│   ├── activities/    # движки: survey, ugc, booking, wheel, photo_frame, confirmation
│   ├── admin_access/  # auth, CSRF, RBAC по admin_brands
│   ├── audit/         # append-only audit_log
│   ├── consent/       # require_consent, CURRENT_POLICY_VERSION
│   ├── channels/      # abstract channel registration
│   ├── jobs/          # enqueue, claim_batch, outbox render
│   ├── metrics/       # daily_counts
│   ├── ports/         # TelegramFilesPort, ObjectStoragePort, ClockPort, RandomPort, …
│   ├── rewards/       # grant_on_completion, list_filtered
│   ├── types/         # DTO + ID types + enums
│   └── users/         # pii_normalize, reveal_user_phone, list_users_page
├── adapters/
│   ├── password/      # Argon2PasswordHasher
│   ├── object_storage/# local_fs, selectel_s3
│   ├── telegram_files/# httpx-based TelegramFiles
│   ├── image_frame/   # PillowImageFrame
│   └── random/        # SystemRandom
├── api/
│   ├── internal/      # HMAC endpoints для tg_worker, workers
│   └── middleware/    # auth_internal, csrf, pii_mask
├── admin/             # Jinja2 UI: /admin/{login,ugc,schedule,users,rewards,metrics}
├── workers/           # jobs worker entry + handlers (ingest, notify_*, polaroid)
├── db/
│   ├── models.py      # SQLAlchemy declarative
│   ├── repositories/  # SqlUserRepo, SqlAttemptRepo, SqlRewardRepo, SqlJobRepo, …
│   ├── unit_of_work.py
│   └── session.py
├── config.py          # pydantic-settings, env-переменные (имена, не значения)
└── main.py            # FastAPI app, routers, lifespan
tg_worker/             # отдельный процесс: aiogram handlers + internal HTTP client
alembic/versions/      # 16 миграций (0001..0016), только обратимые
architecture/          # 11 файлов канона + schema.sql/schema.prisma + 46 ADR в журналах
docker-compose.yml     # dev
docker-compose.prod.yml      # RU-сервер (postgres+app+jobs_worker+caddy)
docker-compose.tg.yml        # 2-й сервер (tg_worker + tinyproxy для TG CDN)
deploy/                # Caddyfile, pg_dump.sh, DEPLOY.md, prod.env.example
```

---

## 5. Запуск локально

```bash
git clone https://github.com/<github-user>/brand_activities_bot.git
cd brand_activities_bot
cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN, INTERNAL_API_AUTH_SECRET, ADMIN_SESSION_SECRET
docker compose up -d                                   # postgres + app + tg_worker (dev)
docker compose exec app python -m alembic upgrade head # миграции
docker compose exec -it app python -m app.admin create_user --login you --display-name "You"
# логин: http://localhost:8000/admin/login
# OpenAPI: http://localhost:8000/docs
```

Dev-команды (на хосте):

```bash
uv run pytest -q                   # 835 passed
uv run ruff check .
uv run mypy app/core
uv run alembic upgrade head
```

Pre-commit hook — локальный, сканирует staged на `.env`, `*.pem`, `*.key`, `.mcp.json`, присвоения `*_SECRET=...` ≥10 символов и блокирует коммит.

---

## 6. Прод-деплой

Пример двухсерверной топологии с SSH-прокси (RU-сервер + внешний сервер для вызовов Telegram API, когда у RU нет исходящего доступа). Детали — [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

---

## 7. Расширяемость — что сделано удобно для будущего

### Добавить новый бренд

1. Новая строка в `brands` (seed-миграция).
2. Привязка `admin_brands` к существующему движку: `activities (slug, display_name, kind, limits)` указывает на survey/ugc/booking/wheel/photo_frame/confirmation.
3. Для survey — вопросы и карточки в `survey_questions.options` и `product_recommendations`.
4. Для UGC — флаги `requires_post_url`, `requires_subscription_url` в `limits` (миграция 0016 — образец).

Кода править не нужно (кроме редких случаев, когда нужен новый вид активности — см. ниже).

### Добавить новый вид активности

1. Новый пакет `app/core/activities/<kind>/` с use-cases (reuse `attempts`, `rewards`, `jobs`).
2. Новая ветка в `tg_worker/handlers/main_menu._launch_activity` по `kind` → `launch_<kind>` (образец — `launch_ugc`).
3. CHECK в миграции на `activities.kind` расширяется отдельной миграцией.
4. Для UI админки (если нужен) — новый роутер + Jinja-шаблон; подключение в `app/admin/router.py` и ссылка в `nav.html`.

Паттерны уже есть: 6 действующих видов — `survey`, `ugc`, `booking`, `wheel`, `photo_frame`, `confirmation`. Новый — делается по образцу за одну фазу.

### Добавить новый мессенджер (MAX, WhatsApp, веб-виджет)

1. Новая папка `app/adapters/<channel>/` с handlers.
2. Новый `channel_type` в `channel_accounts` (миграция добавляет enum или строку — текущая схема допускает произвольные строки).
3. HTTP-эндпоинты `/internal/v1/channels/<channel>/{updates,outbox/claim,outbox/ack}` по образцу `telegram`.
4. Внутренний HMAC — тот же `INTERNAL_API_AUTH_SECRET`.

Ядро не меняется. Проверено архитектурно: `grep "from aiogram\|import aiogram" app/core/` пусто.

### Сменить хранилище UGC

1. В `app/adapters/object_storage/` новая реализация `ObjectStoragePort` (напр. `yandex_s3.py`).
2. В `.env` поменять `OBJECT_STORAGE_IMPL=yandex_s3` + credentials.
3. Миграция не нужна (путь к файлу — строка в `ugc_submissions.file_path`).

### Сменить SMS/email-провайдера

Пока не используется (promocode выдаётся через outbox-сообщение в чат). При появлении — порт в `app/core/ports/notifier.py` + адаптер.

### Horizontal scale

- `app` (uvicorn) stateless → можно поднимать N штук за Caddy.
- `jobs_worker` безопасно скейлится через SKIP LOCKED + `locked_by` fencing.
- Postgres — primary-replica при росте.
- tg_worker singleton (long polling), для масштабирования — переход на webhook (готов — `WEBHOOK_SECRET` в конфиге, нужен HTTPS-эндпоинт `/webhook/telegram/<secret>`).

### Миграция на webhook

Код tg_worker уже разделён на input (handlers) и output (outbox puller). Переход:
1. Поднять HTTPS webhook endpoint в `app` (есть plumbing для `WEBHOOK_SECRET`).
2. `setWebhook` на URL + секретный путь.
3. tg_worker оставить только для outbox puller, или перенести puller в `app`.

---

## 8. Данные и миграции

16 миграций в `alembic/versions/`, все обратимые (`downgrade`). Ключевые объекты:

- `brands` — 5 брендов.
- `activities` — 10+ активностей с JSONB `limits` (бизнес-специфика живёт здесь).
- `users`, `consents`, `channel_accounts` — ПДн изолированы, consent с `text_hash` на версию политики.
- `attempts` — одно прохождение активности (`in_progress` → `completed`/`approved`/`rejected`/`cancelled`/`expired`). Частичный unique-индекс `attempts_once_per_day` (включая `rejected`).
- `rewards` — выданные промокоды, ваучеры, листовки, карточки, призы колеса. CHECK `code IS NOT NULL ↔ reward_type IN (promocode, voucher)`.
- `ugc_submissions` — состояние UGC (`receiving` → `pending` → `approved`/`rejected`/`ingest_failed`). Частичный unique-индекс `ugc_submission_active` страхует от двойной активной подачи.
- `jobs` — очередь задач, claim через SKIP LOCKED, idempotency_key.
- `slots`, `appointments` — booking (Бренд A + Бренд C).
- `wheel_prizes`, `wheel_spins` — колесо; остатки призов атомарным UPDATE с предикатом.
- `frame_submissions` — Polaroid-фото.
- `audit_log` — append-only, actor_admin_id ⊕ actor_user_id.

---

## 9. Тесты и качество

| Что | Инструмент | Статус |
|---|---|---|
| Unit + integration | pytest + pytest-asyncio + httpx + freezegun + testcontainers Postgres | **835 passed** |
| Статика | ruff (E, F, I, W, B, UP, line-length 100) | чисто |
| Типы | mypy strict на `app/core/*` | 93 файла, 0 ошибок |
| Секреты в репо | локальный pre-commit хук (fail-closed) | pass |
| CI | GitHub Actions `.github/workflows/ci.yml`: ruff → mypy → pytest -q на Python 3.12 + uv 0.13.0 | — |

Правило 1 (consent-gate), правило 2 (PII-masking), правило 10 (admin auth) закрыты приёмочными тестами. Миграции тестируются `up/down/up` + проверкой сид-данных.

---

## 10. Процесс разработки

MVP строился по паттерну «оркестратор + исполнители»: главная сессия ставит фазы и пишет детальные промты, исполнители (отдельные Claude-сессии) пишут код и сдают отчёт по шаблону, независимый ревьюер проверяет каждую фазу. Фазу закрывает оркестратор поимённым коммитом + checkpoint. Push, деплой, BotFather — только по явному разрешению владельца.

---

**Автор:** Artem Lavrov. Проект — демо-витрина архитектуры.
