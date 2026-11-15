### Fixed

- A re-converge of a PostgreSQL, Elasticsearch or logging service no longer needs the network for its package repository's signing key: the PostgreSQL, Elastic and Grafana roles download the key only when it is missing (a `get_url` without `force` still asked the server whether the key had changed, so a slow `www.postgresql.org` failed a converge that should change nothing), keep the installed key's owner and mode, and retry a failed download five times, ten seconds apart, before failing (#388)
