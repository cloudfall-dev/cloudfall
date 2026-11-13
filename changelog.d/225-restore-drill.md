### Fixed

- The PostgreSQL restore drill compares the row count of every table the dump holds with the restored table and fails on a difference or on a dump without a table; it counted `pg_class` rows before, which an empty database has too, so a restore that brought back nothing passed (#225)
- The restore drill's scratch database keeps to PostgreSQL's 63-byte names (`<database>_restore_check`, the name cut to 49 characters), and `cloudfall config validate` and the converge refuse a declared database named as another's scratch database, as `CONFIG_INVALID` with code `service_database_restore_check_name` (#225)

### Added

- The PostgreSQL backup writes `globals-<timestamp>.sql` from `pg_dumpall --globals-only --no-role-passwords` beside the per-database dumps, so a restore onto a fresh server has its roles; retention prunes it with the dumps (#225)
