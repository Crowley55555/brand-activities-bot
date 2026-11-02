#!/usr/bin/env bash
# Ежедневный бэкап каталога UGC-фото (`data/ugc`) RU-сервера.
# Запускается из корня репозитория (cron, см. deploy/DEPLOY.md §3).
# Переменные:
#   BACKUPS_DIR               — каталог для бэкапов (по умолчанию ./backups).
#   UGC_LOCAL_PATH            — исходный путь UGC-файлов (по умолчанию ./data/ugc).
#   UGC_BACKUP_RETENTION_DAYS — срок хранения в сутках (по умолчанию 7).
# Поведение: rsync -a --delete в `$BACKUPS_DIR/ugc/<YYYYMMDD>`; старые даты удаляются.
# Актуально для `OBJECT_STORAGE_IMPL=local_fs`. На S3-прод (`selectel_s3`) не нужен —
# S3-бакет бэкапится отдельно средствами провайдера.
set -euo pipefail

BACKUPS_DIR="${BACKUPS_DIR:-./backups}/ugc"
RETENTION_DAYS="${UGC_BACKUP_RETENTION_DAYS:-7}"
SRC="${UGC_LOCAL_PATH:-./data/ugc}"

if [ ! -d "$SRC" ]; then
    echo "ugc backup skip: source $SRC does not exist (local_fs не используется)"
    exit 0
fi

mkdir -p "$BACKUPS_DIR"
STAMP="$(date +%Y%m%d)"
TARGET="$BACKUPS_DIR/$STAMP"

# rsync -a: сохранить время/права; --delete: убрать из бэкапа файлы, удалённые в SRC
# (чтобы /forget каскад (I-2) чистил и бэкап).
rsync -a --delete "$SRC/" "$TARGET/"

# Ретеншн: удаляем дневные папки старше RETENTION_DAYS суток.
find "$BACKUPS_DIR" -maxdepth 1 -mindepth 1 -type d -mtime +"$RETENTION_DAYS" \
    -exec rm -rf {} +

echo "ugc backup ok: $TARGET"
