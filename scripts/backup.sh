#!/bin/sh
set -eu
umask 077
backup_dir=${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$backup_dir"
# Stop writers for a consistent DB + artifact snapshot. Keep PostgreSQL running.
docker compose stop orchestrator discord-gateway upstream-worker downstream-worker
docker compose exec -T postgres pg_dump -U team -d team -Fc > "$backup_dir/database.dump"
docker compose run --rm --no-deps --entrypoint tar orchestrator -C /artifacts -czf - . > "$backup_dir/artifacts.tar.gz"
cp config.yaml "$backup_dir/config.yaml"
echo "Backup complete: $backup_dir. Services remain stopped; restart after checking the backup."
