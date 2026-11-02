# Деплой brand_activities_bot (2 сервера)

Топология: **RU-сервер** = postgres + app + jobs_worker + Caddy (HTTPS) + `/data/ugc`;
**2-й сервер (вне РФ)** = только `tg_worker` (long polling), ходит в RU по HTTPS с HMAC.
Файлы: `docker-compose.prod.yml`, `docker-compose.tg.yml`, `deploy/Caddyfile`,
`deploy/pg_dump.sh`, `deploy/prod.env.example`. Все примеры ниже - с плейсхолдерами
(`<you>`, `<example.ru>`), реальные значения в git не попадают.

Команды выполняются только в фазе 8b, по отдельному «да» владельца.

## 0. Предусловия

- DNS: A-запись `<example.ru>` -> IP RU-сервера (нужна для Let's Encrypt).
- RU-сервер: открыты 80/443; Docker + compose-плагин; исходящий доступ к `api.telegram.org`
  (jobs_worker скачивает присланные пользователями файлы через Bot API; если доступа нет,
  приём UGC не заработает - решить до деплоя).
- 2-й сервер: исходящий доступ к `api.telegram.org` и к `https://<example.ru>`.
- Бот создан в BotFather, токен у владельца.

## 1. RU-сервер

```sh
ssh <user>@<ru-ip>
git clone https://github.com/<github-user>/brand_activities_bot.git && cd brand_activities_bot
cp deploy/prod.env.example .env.prod && chmod 600 .env.prod
# заполнить .env.prod; секреты: openssl rand -hex 32 (пароль БД, HMAC, ADMIN_SESSION_SECRET)
C="docker compose -f docker-compose.prod.yml --env-file .env.prod"
$C build
$C up -d postgres
$C run --rm app python -m alembic upgrade head
$C up -d
$C exec app python -m app.admin create_user --login <you>   # пароль спросит интерактивно
curl https://<example.ru>/health      # {"status":"ok"}
```

Заметки:
- Порт 8000 наружу не публикуется, снаружи доступен только Caddy (80/443).
- Access-лог Caddy выключен намеренно: в URL лежат токены `/media/<id>?t=...`.
- `DATABASE_URL` собирается compose из `POSTGRES_*`, в `.env.prod` его не пишем.

## 2. Второй сервер

```sh
ssh <user>@<second-ip>
git clone https://github.com/<github-user>/brand_activities_bot.git && cd brand_activities_bot
# в .env.tg: TELEGRAM_BOT_TOKEN, INTERNAL_API_URL=https://<example.ru>,
# INTERNAL_API_AUTH_SECRET (тот же, что на RU), TZ=Europe/Moscow
touch .env.tg && chmod 600 .env.tg && nano .env.tg
C="docker compose -f docker-compose.tg.yml --env-file .env.tg"
$C build
$C up -d
$C logs -f tg_worker
```

Проверка: в логах нет ошибок авторизации к RU; `/start` боту -> ответ. `docker compose ps` показывает контейнер `tg_worker` в состоянии `healthy` (фаза 10a, I-5: healthcheck через `INTERNAL_API_URL/health`).

## 3. Бэкапы (RU-сервер)

```sh
(crontab -l 2>/dev/null; cat <<'CRON') | crontab -
0 3 * * * cd /path/to/brand_activities_bot && bash deploy/pg_dump.sh >> backups/pg_dump.log 2>&1
10 3 * * * cd /path/to/brand_activities_bot && bash deploy/ugc_backup.sh >> backups/ugc_backup.log 2>&1
CRON
```

**Postgres** (`deploy/pg_dump.sh`). Хранение 14 дней (`BACKUP_RETENTION_DAYS`). Разовый
прогон: `bash deploy/pg_dump.sh`. Восстановление:
`gunzip -c backups/<file>.sql.gz | $C exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`
(в пустую БД).

**UGC** (`deploy/ugc_backup.sh`, фаза 10b-3, I-6). Для `OBJECT_STORAGE_IMPL=local_fs`
(dev и RU-пилот без S3): `rsync -a --delete` из `$UGC_LOCAL_PATH` (по умолчанию
`./data/ugc`) в `./backups/ugc/<YYYYMMDD>`. Хранение 7 дней
(`UGC_BACKUP_RETENTION_DAYS`). При `selectel_s3` каталог `data/ugc` пустой — скрипт
логирует «skip» и выходит с кодом 0 (S3-бакет бэкапится средствами провайдера).
Разовый прогон: `bash deploy/ugc_backup.sh`.

## 4. Rollback

`git revert <sha>` (или `git checkout <prev>`), затем `$C up -d --build`.
Миграции на prod не откатываем без ручной проверки.

## 5. Smoke (5 брендов)

`/start` -> согласие -> меню -> прохождение Бренд A, Бренд B, Бренд C, Бренд D,
Бренд E; проверить админку `https://<example.ru>/admin/` (вход, список пользователей).

## 6. Диагностика: достижим ли Telegram CDN с RU-сервера

На RU-сервере (ДО `docker compose up`):

```sh
curl -sS --max-time 5 https://api.telegram.org/robots.txt | head -1
# ожидается: любая 200-строка. Таймаут/connection refused → недостижимо.
```

**Решение при недостижимости (чаще всего в РФ):** HTTP-прокси через 2-й сервер.

- На 2-м сервере (NL) `docker-compose.tg.yml` уже включает `tinyproxy` на порту **8443** (3128 нередко блокируется провайдером; 443 обычно занят Caddy/mtg; 8443 стабильный).
- Фаза 10a (I-4): tinyproxy allow-list теперь в самом конфиге, не только в `ufw`. Положи файл по шаблону:
  ```sh
  cp deploy/tinyproxy.conf.example deploy/tinyproxy.conf
  sed -i "s/<RU_SERVER_IP>/$RU_SERVER_IPV4/" deploy/tinyproxy.conf
  chmod 600 deploy/tinyproxy.conf
  grep -E '^Allow' deploy/tinyproxy.conf    # проверка: один Allow с IP RU
  ```
  Повторный запуск `docker compose -f docker-compose.tg.yml --env-file .env.tg up -d tinyproxy` подхватит новый allow-list. Проверка: `curl -x http://<NL_IP>:8443 https://api.telegram.org/robots.txt` → с RU 200, с чужого IP 403.
- Открыть firewall на 2-м сервере только для IP RU (вторая линия защиты):
  ```sh
  apt-get install -y ufw   # на debian/ubuntu
  ufw --force enable
  ufw allow 22/tcp
  ufw allow from <RU_IP> to any port 8443 proto tcp
  ufw status
  ```
- В `.env.prod` на RU добавить (jobs_worker скачает UGC через прокси):
  ```
  HTTPS_PROXY=http://<2ND_SERVER_IP>:8443
  HTTP_PROXY=http://<2ND_SERVER_IP>:8443
  NO_PROXY=postgres,app,localhost,127.0.0.1
  ```
- **Перезапуск jobs_worker строго через `--force-recreate`** (restart НЕ перечитывает `.env`):
  ```sh
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --force-recreate jobs_worker
  ```
- Проверка (из RU):
  ```sh
  docker exec brand_activities_bot-jobs_worker-1 python -c "import httpx; print(httpx.get('https://api.telegram.org/robots.txt', timeout=15).status_code)"
  # ожидается: 200
  ```
- UGC-файлы остаются на RU в `/data/ugc` (local_fs); `/media/<id>` работает.

**Альтернатива (сложнее):** перенести `jobs_worker` на 2-й сервер через `docker-compose.tg.jobs-override.yml` — требует общего S3 (UGC_STORAGE=s3) и открытия 5432/tcp на RU → 2-й.

## 7. Что делает только фаза 8b

`git push`, SSH на серверы, `docker compose up` на prod, BotFather, любые внешние вызовы.
