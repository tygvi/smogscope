#!/bin/sh
# Daily PostgreSQL backup with 7-day retention. Intended for the server.
# Install with:  0 3 * * * /home/ubuntu/smogscope/deploy/backup.sh
set -eu
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/backups}"
mkdir -p "$BACKUP_DIR"

docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
  pg_dump -U smogscope smogscope | gzip > "$BACKUP_DIR/smogscope-$(date +%Y%m%d-%H%M).sql.gz"

find "$BACKUP_DIR" -name 'smogscope-*.sql.gz' -mtime +7 -delete
