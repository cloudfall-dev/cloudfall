### Added

- `deploy` without `--yes` shows the component's release command as `release_command` (its command, the working directory resolved under the new release directory, `timeout_seconds` and the release server), or `null` for a component without one, and says in `would_run` that it runs once on that server before any switch; the `rollback` plan of a component with a release command shows `release_command_skipped: true`, since a rollback never runs it (#120)
