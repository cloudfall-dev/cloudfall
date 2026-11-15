### Security

- `cloudfall data copy` and `cloudfall migrate --data` no longer put the source database URL, password included, on the command line of `psql` and `pg_dump`, where every local user could read it in `/proc/<pid>/cmdline` while the copy ran: the URL becomes a `0600` libpq service file on the target host, named to the clients through `PGSERVICEFILE` and `PGSERVICE` and removed when the copy ends, on failure too. A URL libpq can't read (another scheme, a bad percent-encoding, a parameter set twice) is refused before anything is staged, without quoting it (#355)
