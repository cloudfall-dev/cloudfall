### Fixed

- The PostgreSQL restore drill (`cloudfall backup verify` and the nightly restore-check timer) passes `--file=-` to `pg_restore` when it reads the dump's row counts; without it every PostgreSQL since 12 refuses with `one of -d/--dbname and -f/--file must be specified`, so the drill failed on every run (#340)
