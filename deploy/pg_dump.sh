#!/usr/bin/env bash
# Ежедневный бэкап БД RU-сервера. Запуск из корня репозитория (cron, см. deploy/DEPLOY.md).
# Переменные: BACKUPS_DIR (по умолчанию ./backups), BACKUP_RETENTION_DAYS (по умолчанию 14).
# Логин/БД берутся из окружения контейнера postgres, секреты в скрипт не попадают.
set -euo pipefail

BACKUPS_DIR="${BACKUPS_DIR:-./backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"

mkdir -p "$BACKUPS_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUPS_DIR/backup-$STAMP.sql.gz"
TMP="$TARGET.partial"

# Пишем во временный файл: упавший pg_dump не оставляет «бэкап» с пустым телом.
trap 'rm -f "$TMP"' EXIT
$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > "$TMP"
mv "$TMP" "$TARGET"
chmod 600 "$TARGET"

# Ретеншн: удаляем бэкапы старше RETENTION_DAYS суток.
find "$BACKUPS_DIR" -name 'backup-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
echo "backup ok: $TARGET"
