### Fixed

- On a host running more than one service, such as `h1` in `config/examples` with PostgreSQL and Redis, the Redis backup no longer fails with `Permission denied`: the PostgreSQL, Redis and Elasticsearch roles now create the shared backup root `/var/backups/cloudfall` root-owned `0755` before their own backup directory, instead of letting the first service's directory task create it with its own owner and `0700`, and the next services converge repairs a root already owned by `postgres`; each service's own directory keeps its owner and mode (#342)
