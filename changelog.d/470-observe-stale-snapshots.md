### Fixed

- `cloudfall observe` no longer reads a snapshot an earlier run left behind as this run's: it deletes the requested servers' snapshots before the inspect playbook runs, so a host that failed inspect, or one the run stopped before reaching, is listed `missing` and audit reads it as `unknown` instead of judging its old state; a `--limit` run keeps the other servers' snapshots (#470)
