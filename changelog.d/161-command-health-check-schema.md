### Added

- Components: a `healthCheck` of `type: command` (a required non-empty `command` argv, `timeoutSeconds` 1 to 300 and `attempts` 1 to 20, no other key) validates, and `deploy`, `restart` and `rollback` without `--yes` name the check and its command in their plan's `wouldRun`. The engine does not run the check yet: until it does, `deploy`, `restart`, `rollback` and `health` of such a component fail on their first task, before anything changes on the server, instead of passing an unrun gate (#161)
