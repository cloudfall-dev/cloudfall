### Added

- `cloudfall config upgrade`: rewrites a project from the deprecated `ServerType` names to `Baseline` (moves `server-types/` to `baselines/`, rewrites `kind` and `spec.serverType`), keeping comments and formatting; a second run changes nothing (#198)

### Changed

- The resource every server runs is now `kind: Baseline`, stored under `baselines/`, referenced as `Server.spec.baseline` and checked by `baseline.schema.json` (was `ServerType`, `server-types/`, `spec.serverType`, `server-type.schema.json`); `cloudfall add baseline` declares one and `cloudfall add server --baseline` names it (#198)
- Outputs carry the new names: `inventory show` writes `baselines` and each server's `baseline`, `config validate` counts `Baseline` in `by_kind`, `audit` writes each server's `baseline` and checks `baseline.id` (was `server_type.id`, renamed without an alias because proposals and their verifiers match on the check id), the inspect role records `baseline` in observed server snapshots, the rendered Ansible inventory has the host variable `cloudfall_baseline`, the brownfield catalog variable is `cloudfall_baselines`, and the error code `server_type_entry_duplicate` is `baseline_entry_duplicate` (#198)

### Deprecated

- `kind: ServerType`, the `server-types/` folder, `Server.spec.serverType`, the `cloudfall_server_types` catalog variable and a host's `server_type`, `cloudfall add server-type` and `cloudfall add server --type` keep working until 0.9.0; `cloudfall config validate` warns `DEPRECATED_NAME` for each file using an old name, and a server setting both `serverType` and `baseline` fails with `server_baseline_conflict` (#198)
- Until 0.9.0 the old output names stay beside the new ones with the same values: the rendered Ansible inventory's host variable `cloudfall_server_type`, `inventory show`'s `server_types` and each server's `server_type`, `config validate`'s `by_kind.ServerType`, and `audit`'s per-server `server_type`; an observed server snapshot recorded with `spec.serverType` is still read as `spec.baseline`, and one with both fails with `observation_baseline_conflict` (#198)
