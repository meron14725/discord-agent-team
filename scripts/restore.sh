#!/bin/sh
set -eu
backup_dir=${1:?Usage: scripts/restore.sh BACKUP_DIRECTORY}
# Restore only into a NEW, empty destination DB. Existing objects cause an error.
docker compose stop orchestrator discord-gateway upstream-worker downstream-worker
docker compose up -d --wait postgres
docker compose exec -T postgres pg_restore -U team -d team --exit-on-error < "$backup_dir/database.dump"
docker compose run --rm -T --no-deps --entrypoint tar orchestrator -C /artifacts -xzf - < "$backup_dir/artifacts.tar.gz"
echo 'Restore complete. Review config/secrets and keep the old host stopped before starting this host.'
