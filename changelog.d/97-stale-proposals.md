### Added

- An open proposal whose basis hosts have been observed again since is now reported as stale: `decisions list`, `decisions show` and `cloudfall why` add `stale` and `staleHosts` to each `proposed` decision, `--stale` on `decisions list` and `why` keeps only those, and `decisions approve` names each stale host and its newer observation time before asking for the id. Worked out on read from the observation files; the record and its schema do not change (#26)
