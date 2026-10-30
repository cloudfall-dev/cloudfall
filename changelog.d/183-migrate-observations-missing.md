### Fixed

- `cloudfall migrate --yes` no longer crashes with `HANDLER_CRASHED` when it reaches its `audit` or `verify-routes` step on a project with no server observations: the step fails with `migrate_observations_missing`, naming the missing directory and `cloudfall observe`, and a rerun after `observe` resumes at that step (#183)
