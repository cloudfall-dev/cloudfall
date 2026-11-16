### Fixed

- `config validate` refuses an IPv6 network in a MariaDB service's `remoteAccess.allowedSources` with `service_mariadb_remote_access_ipv6`, naming the source and saying a single IPv6 address (`/128`) or an IPv4 network is accepted. Before, such a config validated, the firewall rendered a rule for it, and the converge refused it only after the host had made its certificate key. MariaDB's account host has no netmask form for IPv6, so an IPv6 network never converged; PostgreSQL keeps accepting IPv6 networks (#434)
