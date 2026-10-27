# S-005: Release command

status: built

## Problem

`cloudfall deploy` runs the `cloudfall_deploy` role, which unpacks the release, runs
`uv sync --frozen --no-dev`, switches `current`, restarts the unit and gates on the health
check (`engine/ansible/roles/cloudfall_deploy/tasks/main.yml`). Nothing runs a declared
command against the new release before traffic moves to it, and
`config/schemas/v1/component.schema.json` has no field for one: `service.command` is the
only command a component declares. Database migrations therefore run by hand over SSH after
the health gate (cloudfall-dev/cloudfall#88). The gate checks code against the old schema, a
failed migration leaves no receipt and stops nothing, and the migration's environment is
assembled by hand instead of from the unit's `EnvironmentFile`. This spec pairs only loosely
with S-004: the two share the plan and receipt shape, nothing else.

## Behaviour

A component may declare a release command:

```yaml
spec:
  release:
    command: [.venv/bin/alembic, upgrade, head]
    workingDirectory: services/api
    timeoutSeconds: 300
```

- `command` is required, a non-empty list of non-empty strings like `service.command`. A relative first element resolves against the new release directory, as the unit resolves `service.command` against `current`
- `workingDirectory` is optional, a relative path inside the release directory with no `..` segment (the rule `repository.subdirectory` follows). It defaults to the release directory itself
- `timeoutSeconds` is optional, an integer from 1 to 1800, default 300. The bound stays well under `STEP_TIMEOUT_SECONDS` (3600), the limit on the whole playbook run
- A component without `release` deploys exactly as today

**When it runs**

The `cloudfall_deploy` role runs the command after `Materialize locked dependencies for the
release` and before `Install the managed service unit`, so before `current` moves and before
the unit file changes. It runs:

- as the application's `linuxUser` (`application_user` in the rendered inventory)
- in `<installRoot>/releases/<release>`, or in `workingDirectory` under it
- with the environment the unit gets: `<installRoot>/shared/env/<component>.env`, the file the unit names in `EnvironmentFile=-` and that `deploy --env-file` installs. The file is parsed by systemd's own rules (for example a transient `systemd-run` unit with the same `EnvironmentFile=`), so quoting means what it means for the service. A missing file gives no extra variables, as for the unit. The variables from `environment` and `secretRefs` are there when the operator rendered them with `secrets render` and passed the file to this or an earlier deploy

**Once per deploy**

`deploy.yml` walks `cloudfall_servers` one host at a time (`serial: 1`, `any_errors_fatal`)
in inventory order, which need not be `deployment.servers` order. The release server is the
first entry of `deployment.servers`. The deploy runs that server first, and the command runs
there only. The other servers deploy afterwards, one at a time as today, and skip the
command. So the command has run before any server's `current` moves.

The command must be safe while the old release still serves traffic: the other servers, and
the release server itself until its switch, run old code against whatever the command
changed. Migrations should be expand and contract: add first, remove in a later release. A
later failure (a health gate on the second server, say) does not undo what the command did.

**Failure**

A non-zero exit, or a run still going after `timeoutSeconds` (it is then stopped), fails the
release server's play. Because the play is `any_errors_fatal`, no server switches `current`,
no unit file changes, and no service restarts. `deploy --yes` exits `ENGINE_STEP_FAILED`, and
its `error.message` names the release command, its exit code or the timeout, and the end of
its output. The unpacked release directory stays, as any unpacked release does. No release
receipt is written: a `ReleaseReceipt` means a release went live (`healthCheckPassed` is
`const: true`).

**Receipt**

`ReleaseReceipt` (`config/schemas/v1/release-receipt.schema.json`) gains an optional
`spec.releaseCommand` object:

- `server`: the server the command ran on
- `command` and `workingDirectory`, as declared
- `exitCode`: 0
- `outputTail`: the last 2000 characters of its combined stdout and stderr, the bound `lifecycle.py` already uses for step output, with `maxLength: 2000` in the schema

Every receipt the deploy writes carries the same `releaseCommand`, the one from the release
server, so the receipt left at `<releases>/<component>.json` names where the command ran
whichever server wrote it last. A deploy of a component without `release` writes no
`releaseCommand`. Nothing in the SDK loads release receipts today, so an older `cloudfall`
is not affected.

**Plan and rollback**

- `deploy` without `--yes` adds `releaseCommand` to its plan: `command`, `workingDirectory`, `timeoutSeconds` and `server` (the release server), or `null` when the component declares none. Its `wouldRun` says the command runs once on that server before any switch
- `rollback` never runs the release command: downgrades stay a human decision. For a component that declares one, the `rollback` plan's `wouldRun` says the release command is not run and adds `releaseCommandSkipped: true`
- `restart` never runs it either
- `migrate`'s `deploy:<component>` steps go through the same deploy, so they run the command

Touches `config/schemas/v1/component.schema.json`,
`config/schemas/v1/release-receipt.schema.json`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/lifecycle.py`,
`sdk/src/cloudfall/commands.py`, `engine/src/cloudfall_engine/ansible_inventory.py`,
`engine/ansible/playbooks/deploy.yml`, `engine/ansible/roles/cloudfall_deploy`, `deploy`,
`rollback` and the MCP tools generated from them.

## Acceptance criteria

- S-005-1: A component with `release.command` that creates a table deploys with `cloudfall deploy --yes`, the table exists afterwards, and the release receipt has `releaseCommand` with the command, `exitCode: 0` and an `outputTail`
- S-005-2: The command runs after the `Materialize locked dependencies for the release` task and before the `Install the managed service unit` and `Switch the current release symlink` tasks, as the application's `linuxUser`, with the new release directory (or its `workingDirectory`) as its working directory
- S-005-3: A release command that prints every variable sees each key of the installed `<installRoot>/shared/env/<component>.env`, values with the same quoting rules the unit applies
- S-005-4: A release command that exits 1 makes `deploy --yes` exit `ENGINE_STEP_FAILED` with an `error.message` naming the release command and exit code 1; `current` still points at the previous release on every server, the unit file is unchanged, the service was not restarted, and no release receipt was written
- S-005-5: A release command still running after `timeoutSeconds` is stopped and fails the deploy the same way, with an `error.message` naming the timeout
- S-005-6: With `deployment.servers: [b, a]`, the command runs once, on `b`, before either server's `current` moves, and every release receipt from that deploy names `b` in `releaseCommand.server`
- S-005-7: `releaseCommand.outputTail` holds at most the last 2000 characters of a command that prints more
- S-005-8: `deploy` without `--yes` exits 0, shows `releaseCommand` with `command`, `workingDirectory`, `timeoutSeconds` and `server`, and runs no `ansible-playbook`; for a component without `release` it shows `releaseCommand: null`
- S-005-9: `rollback` of a component with `release` runs no release command, and its plan without `--yes` shows `releaseCommandSkipped: true`
- S-005-10: A component without `release` deploys with the same Ansible tasks as before and a receipt with no `releaseCommand`
- S-005-11: `config validate` exits `CONFIG_INVALID` for a `release.workingDirectory` that is absolute or contains `..`, an empty `release.command`, or a `timeoutSeconds` outside 1 to 1800

## Out of scope

- A post-switch hook: the issue's case needs the schema before the gate, and a hook after it can be its own spec
- Running the command on rollback, or a down command: downgrades stay a human decision, as the issue asks
- A failure receipt: a `ReleaseReceipt` means a live release, and its schema says so with `healthCheckPassed: true`. The failure's evidence is the `ENGINE_STEP_FAILED` message. A spec for failed-run records can add one for every engine step at once
- Redacting `outputTail`: the command is the operator's own and runs with secrets in its environment. A command that prints secrets puts them in a receipt under the project, so it should not print them
- `cloudfall run <component> -- <command>` for one-off commands: useful on its own, but it leaves ordering with the deploy to the operator
- Runtimes other than Python with uv: the deploy role supports only those today
- Choosing the release server by anything but its place in `deployment.servers`: the first entry is explicit and needs no new field

## Decisions relied on

- none

## Issues

- cloudfall-dev/cloudfall#119: S-005-1, S-005-2, S-005-3, S-005-4, S-005-5, S-005-6, S-005-7, S-005-10, S-005-11
- cloudfall-dev/cloudfall#120: S-005-8, S-005-9

## Verification

No criterion has run against a live Debian host yet: the role's release command tasks ran under `ansible-playbook` on the controller with a stand-in for `systemd-run`, and `deploy.yml` ran against two local hosts with a recording role. The real `systemd-run` (exit code, the `Finished with result: timeout` line, `EnvironmentFile=` quoting) and `current`, unit and restart state on real servers are unexercised.

- S-005-1: `test_s005_1_a_passing_release_command_records_its_run` and `test_s005_1_stderr_is_part_of_the_recorded_output`, the stand-in command created its table and the recorded run had the command, `exitCode: 0` and the combined output tail
- S-005-2: `test_s005_2_the_release_command_runs_between_materialize_and_the_switch` (task order in the role) and `test_s005_2_the_command_runs_in_its_working_directory_as_the_user` (working directory, `--uid`/`--gid` of the application user)
- S-005-3: `test_s005_3_the_command_loads_the_unit_environment_file`, the `EnvironmentFile=` line equals the unit template's
- S-005-4: `test_s005_4_a_failing_release_command_fails_the_play_naming_its_exit`, `test_s005_4_a_unit_that_never_starts_names_systemd_run_s_reason` and `test_s005_4_a_failed_release_server_stops_every_other_server`, the message named the command and exit code 1 and no other server ran
- S-005-5: `test_s005_5_a_timed_out_release_command_fails_naming_the_timeout` and `test_s005_5_the_timeout_is_the_unit_runtime_limit` (`RuntimeMaxSec=`)
- S-005-6: `test_s005_6_the_release_server_deploys_first_and_runs_the_command_once` with `deployment.servers: [h2, h1]`, plus `test_s005_6_only_the_release_server_runs_the_command`, `test_s005_6_a_server_whose_release_server_has_not_passed_does_not_switch`, `test_s005_6_every_receipt_names_the_release_server`, `test_s005_6_the_release_server_is_the_first_deployment_server` and `test_s005_6_a_release_renders_with_its_release_server`
- S-005-7: `test_s005_7_the_output_tail_keeps_the_last_2000_characters` and `test_s005_7_a_receipt_refuses_an_output_tail_over_2000_characters`
- S-005-8: `test_s005_8_the_deploy_plan_shows_the_release_command`, `test_s005_8_the_plan_resolves_a_declared_working_directory_and_timeout` and `test_s005_8_a_component_without_release_shows_a_null_release_command`, `deploy` without `--yes` exited 0 with `release_command` (`working_directory` resolved to `<installRoot>/releases/<release>`, `timeout_seconds: 300` by default, `server: h2`) or `null`; the CLI pointed at an engine whose playbooks only leave a marker left none and rendered no inventory, while the same engine with `--yes` did leave one (`test_s005_8_the_marker_engine_does_record_a_playbook_run`)
- S-005-9: `test_s005_9_the_rollback_plan_skips_the_release_command` showed `release_command_skipped: true`, `test_s005_9_a_rollback_without_release_skips_nothing` showed `null`, and `test_s005_9_the_rollback_run_includes_no_release_command_task` found no release command task in `rollback.yml` or what it includes
- S-005-10: `test_s005_10_a_component_without_release_runs_no_new_task`, `test_s005_10_a_receipt_without_release_has_no_release_command`, `test_s005_10_a_component_without_release_deploys_in_inventory_order` and `test_s005_10_a_component_without_release_serializes_as_before`
- S-005-11: `test_s005_11_config_validate_refuses_a_malformed_release`, `test_s005_11_config_validate_accepts_a_declared_release` and `test_s005_11_release_values_are_refused_outside_their_bounds`
