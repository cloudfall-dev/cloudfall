### Fixed

- `cloudfall-engine inventory render --output` keeps the `cloudfall_servers` hosts in rollout order instead of sorting them by name, so a `serial: 1` play run from the file converges a multi-node Elasticsearch cluster's home and each MariaDB primary's server first, as S-012 and S-019 require, whatever the servers are named. Every other key in the file stays sorted, so a project without such a home renders the same file as before. The stdout answer holds the same inventory, with its keys sorted as every treaty answer's are (#465)
