### Added

- `cloudfall domains observe <domain>` reads one Domain on its proxy server alone, through the new read-only `domains-observe.yml`: the vhost file and its sha256, the certificate's expiry, `certbot.timer`, and whether the vhost still matches the last receipt (`receipt: null` before the first). It changes nothing on the server or in the project, and a missing vhost exits 0. `cloudfall mcp serve` lists `domains_apply` (with its `yes` gate) and `domains_observe` as tools (#118)
