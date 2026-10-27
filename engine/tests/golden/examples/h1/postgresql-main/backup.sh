#!/usr/bin/env bash
# Managed by Cloudfall. Local changes will be overwritten.
set -euo pipefail

backup_directory="/var/backups/cloudfall/postgresql-main"
port="5432"
retention_days="14"
timestamp="$(date --utc +%Y%m%dT%H%M%SZ)"

pg_dump --port "${port}" --format=custom \
  --file "${backup_directory}/crm-${timestamp}.dump" \
  "crm"

find "${backup_directory}" -name '*.dump' -type f \
  -mtime "+${retention_days}" -delete
