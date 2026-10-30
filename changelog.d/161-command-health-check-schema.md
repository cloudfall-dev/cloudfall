### Added

- Components: a `healthCheck` of `type: command` (a required non-empty `command` argv, `timeoutSeconds` 1 to 300 and `attempts` 1 to 20, no other key) validates, and `deploy`, `restart` and `rollback` without `--yes` name the check and its command in their plan's `wouldRun` (#161)
