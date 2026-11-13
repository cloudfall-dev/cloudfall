### Added

- `deploy`, `rollback`, `restart` and an artifact build hold their component's lock under `tmp/locks/` on the management host until they end, passed, failed or interrupted, so a second run of the same component exits `CONFLICT` (`component_locked`) before any server is contacted, naming who holds the lock, since when, and whether that process still runs; `cloudfall lock clear <component>` shows a lock a killed run left, with its age, and removes it once a person types the component id at a terminal, never while its process still runs on this host (#221)
