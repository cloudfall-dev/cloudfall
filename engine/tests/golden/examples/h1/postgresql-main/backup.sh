#!/usr/bin/env bash
# Managed by Cloudfall. Local changes will be overwritten.
set -euo pipefail

backup_directory="/var/backups/cloudfall/postgresql-main"
port="5432"
retention_days="14"
timestamp="$(date --utc +%Y%m%dT%H%M%SZ)"

# Roles and other globals, which no pg_dump of one database holds. Role
# passwords stay out: converge sets each owner role's from its secret.
pg_dumpall --port "${port}" --globals-only --no-role-passwords \
  --file "${backup_directory}/globals-${timestamp}.sql"
pg_dump --port "${port}" --format=custom \
  --file "${backup_directory}/crm-${timestamp}.dump" \
  "crm"

find "${backup_directory}" -type f \
  \( -name '*.dump' -o -name 'globals-*.sql' \) \
  -mtime "+${retention_days}" -delete
