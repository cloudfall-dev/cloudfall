### Fixed

- A `pg_hba.conf` reload that failed is no longer forgotten: `services.yml` leaves a marker file at `/var/lib/cloudfall/postgresql/<service>.pg_hba-reload-pending` when it closes remote access after a failed check or reload, and the next converge checks and reloads `pg_hba.conf` even though the file is unchanged, then removes the marker, so the old `hostssl` lines no longer stay live until a restart (#144)
- `cloudfall config validate` refuses an owned database or owner named `all`, `sameuser`, `samerole`, `samegroup` or `replication` on a PostgreSQL service with `remoteAccess`, as `CONFIG_INVALID` with code `service_remote_access_name_reserved`, instead of leaving the refusal to the converge; without `remoteAccess` those names still validate (#144)
