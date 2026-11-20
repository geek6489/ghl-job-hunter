#!/usr/bin/env sh
# Manual Postgres backup: DATABASE_URL=postgresql://... ./scripts/backup.sh
set -eu
mkdir -p backups
pg_dump "${DATABASE_URL:?set DATABASE_URL}" | gzip > "backups/jobs-$(date +%F-%H%M).sql.gz"
echo "backup written to backups/"
