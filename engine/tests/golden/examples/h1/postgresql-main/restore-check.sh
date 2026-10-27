#!/usr/bin/env bash
# Managed by Cloudfall. Local changes will be overwritten.
# Restores the newest dump of every declared database into a scratch
# database, counts catalog rows, and drops the scratch database again.
set -euo pipefail

backup_directory="/var/backups/cloudfall/postgresql-main"
port="5432"

latest="$(find "${backup_directory}" -name 'crm-*.dump' \
  -type f | sort | tail -n 1)"
if [ -z "${latest}" ]; then
  echo "no backup found for crm" >&2
  exit 1
fi
scratch="crm_restore_check"
dropdb --port "${port}" --if-exists "${scratch}"
createdb --port "${port}" "${scratch}"
pg_restore --port "${port}" --dbname "${scratch}" --no-owner \
  --exit-on-error "${latest}"
relations="$(psql --port "${port}" --tuples-only --no-align \
  --command 'SELECT count(*) FROM pg_class' "${scratch}")"
dropdb --port "${port}" "${scratch}"
echo "restore check passed for crm:" \
  "${latest} (${relations} catalog relations)"
