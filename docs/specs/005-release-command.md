# S-005: Release command

status: approved

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

## Verification
