# S-009: Command health check

status: built
serves: G-3

## Problem

A component's health gate can check one local URL for an expected status (`healthCheck`
`type: http`) or only that the unit stays up (`type: none`)
(`config/schemas/v1/component.schema.json`, `healthCheck` `oneOf`). A smoke check that
makes authenticated calls to the routes customers use can't gate a deploy: `release.command`
(S-005) runs before the switch, so it can't test the new release while it serves, and nothing
after the switch in "Activate the release behind the health gate"
(`engine/ansible/roles/cloudfall_deploy/tasks/main.yml`) can fail the deploy and take its
`rescue` (cloudfall-dev/cloudfall#152). Teams run the smoke check as a separate CI step after
`cloudfall deploy` and call `cloudfall rollback` themselves, so a bad release serves for that
window and its rollback is not in the release receipt. In the reported case the smoke check
caught a 500 on every authenticated route that an HTTP check on `/health` passed.

## Behaviour

A component may declare a command health check:

```yaml
spec:
  healthCheck:
    type: command
    command: [.venv/bin/python, -m, app.smoke]
    timeoutSeconds: 60
    attempts: 3
```

- `command` is required, a non-empty list of non-empty strings like `service.command`. A relative first element resolves against `<installRoot>/current/`, as the unit template (`component.service.j2`) resolves `service.command`; an absolute one is used as is
- `timeoutSeconds` is required, an integer from 1 to 300, the bound of the `http` check; it limits each run
- `attempts` is required, an integer from 1 to 20, the bound of the `http` check
- No other key is accepted (`scheme`, `port`, `path` and `expectedStatuses` belong to `http`)
- Components with `type: http` or `type: none` deploy, restart, roll back and report health exactly as today

**How it runs**

In the `cloudfall_deploy` role, the check runs inside "Activate the release behind the
health gate", after `Switch the current release symlink` and `Restart the component
service`, on each server as that server's gate (the play stays `serial: 1`,
`any_errors_fatal`). It reuses the transient unit mechanism of `release_command.yml`: a
`systemd-run --wait --collect --service-type=exec` unit named
`cloudfall-health-<component>`, with:

- `--uid` and `--gid` of the application's `linuxUser` (`application_user`)
- working directory `<installRoot>/current`
- `EnvironmentFile=-<installRoot>/shared/env/<component>.env`, the line the service unit carries, so the command sees the variables the service sees, parsed by systemd's rules; a missing file gives no extra variables
- `RuntimeMaxSec=` set to `timeoutSeconds`, `TimeoutStopSec=10`, `NoNewPrivileges=true` and `PrivateTmp=true`, as for the release command

It runs up to `attempts` times, 3 seconds apart, the delay the `http` check uses, and stops
at the first exit 0. Each run is bounded by `timeoutSeconds`; a run still going then is
stopped and counts as a failed attempt. The check is skipped in check mode, like the other
gates. The tasks live in one task file, `engine/ansible/roles/cloudfall_deploy/tasks/command_health_check.yml`,
included from the gate block in `main.yml` and from `health_gate.yml`; it computes its paths
from `install_root`, because `restart.yml` and `rollback.yml` don't set the deploy's path
facts.

**Output stays on the server**

The command's stdout and stderr go to the server's journal under the transient unit's name
(no `--pipe`), where the service's own logs already are; `journalctl -u
cloudfall-health-<component>` on the server shows them. The run task is `no_log: true`. So
the command's output never reaches the controller: not the receipt, not the
`ansible-playbook` output, not the `deploy` error. Cloudfall reads only `systemd-run`'s exit
code and its own `Finished with result: timeout` line, which tells a timeout from an exit.

**Failure**

When every attempt fails, the gate fails and takes the existing `rescue`: the previous
release's symlink is restored and the service restarted, or, for a first release, the
service is stopped. Before the rescue fails the play, it writes a failed release receipt (see
Receipt). `deploy --yes` exits `ENGINE_STEP_FAILED`, and its `error.message` says the command
health check of the component failed on that server, with the last attempt's exit code or
the timeout, the number of attempts, and the release it was rolled back to (or that it was
stopped). A server that already passed its gate earlier in the same deploy keeps the new
release, as with an `http` check today.

**Receipt**

`ReleaseReceipt` (`config/schemas/v1/release-receipt.schema.json`) gains an optional
`spec.healthCheck` object, present only for a component with a command check:

- `type`: `command`
- `command`: as declared
- `attempts`: how many runs the gate made, 1 to 20
- `exitCode`: the last run's exit code, or `null` when it timed out
- `timedOut`: whether the last run was stopped at `timeoutSeconds`

The object takes no other key (`additionalProperties: false`), so the schema itself refuses a
receipt that carries output. `healthCheckPassed` changes from `const: true` to a boolean;
`false` is valid only with a `healthCheck` of `type: command` whose `exitCode` is not 0. On a
pass the receipt is written where and when it is today, with `healthCheckPassed: true` and
`exitCode: 0`. On a failed command check the rescue writes the receipt to the same path,
`<releases>/<component>.json`, with `healthCheckPassed: false`; `release` names the release
that failed, `previousRelease` names the release now active again (`null` for a first release,
which was stopped), and `releaseCommand` is there as on a pass when the component declares one.
The receipt file is the record of the component's last deploy on the server that wrote it last,
so `previousRelease` is what a reader checks to know what serves after a failure. A failed
`http` or `none` gate writes no receipt, as today. Nothing in the SDK loads release receipts
today, so an older `cloudfall` is not affected.

**Restart, rollback and health**

`restart.yml`, `rollback.yml` and `health.yml` include `health_gate.yml`, so `cloudfall
restart`, `cloudfall rollback` and `cloudfall health` run the command check too: one
declared check, one meaning, wherever Cloudfall asks whether the component is healthy. They
write no receipt, as today. A rollback still does not restore anything when its gate fails.
The check must therefore be safe to run at any time against the live service, as a smoke
check is.

**Plan**

`deploy`, `restart` and `rollback` without `--yes` say in their `effect` that the component's
command health check `<command>` runs on each server after its restart. The plan JSON gains no
field.

Touches `config/schemas/v1/component.schema.json`,
`config/schemas/v1/release-receipt.schema.json`, `sdk/src/cloudfall/domain.py`
(`HealthCheckType`), `sdk/src/cloudfall/inventory.py` (`ComponentHealthCheck` and its parser),
`sdk/src/cloudfall/lifecycle.py` (preview effects),
`engine/src/cloudfall_engine/ansible_inventory.py`,
`engine/ansible/roles/cloudfall_deploy/meta/argument_specs.yml`,
`engine/ansible/roles/cloudfall_deploy/tasks/main.yml`,
`engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml`, the new
`engine/ansible/roles/cloudfall_deploy/tasks/command_health_check.yml`, `engine/README.md`,
`config/README.md` and `docs/ci-deploy-guide.md`.

## Acceptance criteria

- S-009-1: `config validate` accepts a component with `healthCheck: {type: command, command, timeoutSeconds, attempts}` and exits `CONFIG_INVALID` for an empty `command`, a missing `timeoutSeconds` or `attempts`, a `timeoutSeconds` outside 1 to 300, an `attempts` outside 1 to 20, or an `http` key such as `port` on a command check
- S-009-2: The command check runs after the `Switch the current release symlink` and `Restart the component service` tasks, inside the block whose `rescue` restores the previous release, as the application's `linuxUser`, with working directory `<installRoot>/current`, the service unit's `EnvironmentFile=-` line and `RuntimeMaxSec=` equal to `timeoutSeconds`
- S-009-3: A relative first element of `command` runs as `<installRoot>/current/<element>`, and an absolute one runs unchanged
- S-009-4: A command check that exits 0 lets `deploy --yes` exit 0 with a receipt carrying `healthCheckPassed: true` and `healthCheck` with the command, `attempts: 1`, `exitCode: 0` and `timedOut: false`
- S-009-5: A command check that exits 3 on every attempt makes `deploy --yes` exit `ENGINE_STEP_FAILED` with an `error.message` naming the command health check and exit code 3; afterwards `current` points at the previous release, the service was restarted on it, and the receipt at `<releases>/<component>.json` validates against the schema with `healthCheckPassed: false`, `healthCheck.exitCode: 3`, `release` the failed release and `previousRelease` the restored one
- S-009-6: The same failure on a first release stops the service and writes a receipt with `healthCheckPassed: false` and `previousRelease: null`
- S-009-7: A run still going after `timeoutSeconds` is stopped and counts as a failed attempt; when the last attempt timed out, the deploy fails the same way, the `error.message` names the timeout, and the receipt has `timedOut: true` and `exitCode: null`
- S-009-8: A check that fails twice and then passes with `attempts: 3` passes and records `attempts: 3`; a check that always fails runs exactly `attempts` times, 3 seconds apart
- S-009-9: A check that prints a marker string on stdout and stderr and exits 1 leaves the marker in neither the receipt, the `ansible-playbook` output nor the `deploy` error, and the receipt schema refuses a `healthCheck` with any key besides `type`, `command`, `attempts`, `exitCode` and `timedOut`
- S-009-10: `restart`, `rollback` and `health` of a component with a command check run the same check; `health` reports `unhealthy` when it exits non-zero, and none of them writes a receipt
- S-009-11: The receipt schema refuses `healthCheckPassed: false` without a command `healthCheck`, or with `exitCode: 0`, and still accepts every receipt the deploy writes today
- S-009-12: Components with `type: http` and `type: none` run the same gate tasks as before, their receipts have no `healthCheck` key, and a failed `http` or `none` gate writes no receipt
- S-009-13: `deploy`, `restart` and `rollback` without `--yes` exit 0, name the command health check and its command in `effect` for a component with one, and run no `ansible-playbook`

## Out of scope

- A failed receipt for `http` and `none` gates: kept as today so existing components and their receipts don't change; a later spec can make every failed gate leave one
- Recording any of the command's output, even a capped or redacted tail: it may hold the secrets it authenticates with. The output is in the server's journal
- A configurable delay between attempts: fixed at 3 seconds like `http`
- A `workingDirectory` for the check: it runs in `current`, as the service does; a command can change directory itself
- Combining checks (an `http` check and a command check together, or several commands): one `healthCheck` per component, as today
- Running the check from the controller or another server: it runs on the server it gates
- A history of receipts: `<releases>/<component>.json` keeps only the last deploy, as today; `docs/ci-deploy-guide.md` already shows how to keep each one

## Decisions relied on

- none

## Issues

- cloudfall-dev/cloudfall#161: S-009-1, S-009-13
- cloudfall-dev/cloudfall#164: S-009-2, S-009-3, S-009-4, S-009-5, S-009-6, S-009-7, S-009-8, S-009-9, S-009-11, S-009-12
- cloudfall-dev/cloudfall#166: S-009-10

## Verification

No criterion has run against a live Debian host yet: the role's command check, receipt, `health` and shared gate tasks ran under `ansible-playbook` on the controller with a stand-in for `systemd-run`, and the gate block's order and rescue were replayed with Ansible's own templating. The real `systemd-run` (its exit code, the `Finished with result: timeout` line, `EnvironmentFile=` parsing, the journal), `become` on the `health` play, and `current`, unit and restart state on real servers are unexercised.

- S-009-1: `test_s009_1_config_validate_accepts_a_command_health_check`, `test_s009_1_config_validate_accepts_the_bounds`, `test_s009_1_config_validate_refuses_a_malformed_command_check`, `test_s009_1_the_timeout_refuses_values_outside_1_to_300`, `test_s009_1_attempts_refuse_values_outside_1_to_20`, `test_s009_1_a_check_carries_exactly_the_contract_of_its_type`, `test_s009_1_a_command_health_check_renders_as_declared` and `test_s009_1_the_role_argument_spec_accepts_every_declared_check`
- S-009-2: `test_s009_2_the_check_runs_after_the_restart_inside_the_rescued_block` (task order and the rescue) and `test_s009_2_the_check_runs_as_the_user_in_current_with_the_unit_environment` (`--uid`/`--gid`, working directory, the unit's `EnvironmentFile=-` line, `RuntimeMaxSec=60`)
- S-009-3: `test_s009_3_a_relative_first_element_runs_from_current` and `test_s009_3_an_absolute_first_element_runs_unchanged`
- S-009-4: `test_s009_4_a_passing_check_receipts_its_run`, the receipt validated with `healthCheckPassed: true`, `attempts: 1`, `exitCode: 0` and `timedOut: false`
- S-009-5: `test_s009_5_a_check_exiting_3_fails_and_receipts_the_failed_release`, `test_s009_5_a_failed_check_rolls_back_receipts_then_fails` and `test_s009_5_the_deploy_error_names_the_check_its_exit_and_the_rollback`
- S-009-6: `test_s009_6_a_failed_first_release_receipts_no_previous_release` and `test_s009_6_a_failed_first_release_is_stopped_and_receipted`
- S-009-7: `test_s009_7_a_timed_out_check_fails_naming_the_timeout`, `test_s009_7_a_timed_out_run_counts_as_a_failed_attempt` and `test_s009_7_the_deploy_error_names_the_timeout`
- S-009-8: `test_s009_8_a_check_failing_twice_then_passing_records_three_attempts`, `test_s009_8_a_check_always_failing_runs_exactly_attempts_times_3s_apart` and `test_s009_8_a_passing_first_attempt_stops_there`
- S-009-9: `test_s009_9_the_output_reaches_neither_the_receipt_nor_the_ansible_output` (at `-vvvv`), `test_s009_9_the_run_is_no_log_and_pipes_nothing` and `test_s009_9_the_receipt_schema_refuses_any_other_health_check_key`
- S-009-10: `test_s009_10_restart_rollback_and_health_end_on_the_shared_gate`, `test_s009_10_the_shared_gate_runs_deploys_command_check_as_root`, `test_s009_10_a_passing_check_passes_with_deploys_invocation` and `test_s009_10_a_failing_check_fails_without_its_output_or_a_receipt` (the role's `health` and `health_gate` tasks), `test_s009_10_a_timed_out_check_fails_naming_the_timeout`, `test_s009_10_health_reports_unhealthy_when_the_check_exits_non_zero` and `test_s009_10_health_reports_ok_when_the_check_exits_0` (`cloudfall health` over the role's tasks), `test_s009_10_no_entry_point_includes_the_receipt` and `test_s009_10_no_entry_point_refuses_a_command_check`
- S-009-11: `test_s009_11_a_failed_receipt_needs_a_failed_command_check`, `test_s009_11_a_command_check_record_must_be_coherent`, `test_s009_11_a_passed_receipt_refuses_a_failed_command_check`, `test_s009_11_every_receipt_written_today_still_validates` and `test_s009_11_a_timed_out_failure_validates`
- S-009-12: `test_s009_12_http_and_none_run_the_same_gate_tasks`, `test_s009_12_a_failed_http_or_none_gate_writes_no_receipt`, `test_s009_12_http_and_none_receipts_have_no_health_check`, `test_s009_12_the_pass_receipt_is_written_where_it_was` and `test_s009_12_restart_rollback_and_health_keep_their_http_and_none_gates`
- S-009-13: `test_s009_13_the_plan_names_the_command_health_check` and `test_s009_13_the_plan_gains_no_field`
