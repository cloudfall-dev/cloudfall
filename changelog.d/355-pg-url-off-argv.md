### Security

- `cloudfall data copy` and `cloudfall migrate --data` no longer put the source database URL, password included, on the command line of `psql` and `pg_dump`, where every local user could read it in `/proc/<pid>/cmdline` while the copy ran: the URL becomes a `0600` libpq service file on the target host, named to the clients through `PGSERVICEFILE` and `PGSERVICE` and removed when the copy ends, on failure too. A URL libpq can't read (another scheme, a bad percent-encoding, a parameter set twice) is refused before anything is staged, without quoting it (#355)

### Breaking

- Some source URLs that `cloudfall data copy` and `cloudfall migrate --data` read through `--dbname` before are now refused, because a libpq service file can't hold them as written: a `#` in the database name or a query parameter value, a password that ends in whitespace, and a value holding a line break. The copy stops before anything is staged, with `lifecycle_execution_failed` and a reason that names the URL part without quoting it. Before upgrading, percent-encode a `#` there as `%23`; a password that ends in whitespace or holds a line break can't be staged even percent-encoded, so change that password first (#355)
