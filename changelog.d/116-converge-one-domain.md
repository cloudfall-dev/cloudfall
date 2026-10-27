### Added

- The `converge_domains` MCP tool takes an optional `domain`: given, `domains.yml` runs only on that Domain's proxy server (`--limit`) and renders only that Domain, and an unknown id exits `PRECONDITION` (`lifecycle_domain_missing`) before the inventory render; omitted, it runs on every server as before. Each domain receipt now records `previousConfigurationSha256`, the sha256 of the vhost file the run replaced, or `null` when none was there; receipts without it still load (#116)
