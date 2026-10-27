# S-004: Domains apply

status: built

## Problem

A declared Domain reaches its server only through `domains.yml`. The CLI runs that playbook
only inside `cloudfall migrate` (`sdk/src/cloudfall/migrate.py`, steps `domains-http`,
`dns-verify` and `domains-tls`), and the same plan converges `baseline` and `services` on
every server first. The `converge_domains` MCP tool runs `domains.yml` without the baseline,
but it takes no domain argument and connects to every server in `cloudfall_servers`. So an
operator who may deploy to a server but must not converge its baseline (a server lent from
another project, cloudfall-dev/cloudfall#87) has no safe way to apply one Domain. The vhost
and the certificate get written by hand, outside the receipts, and the next `domains.yml`
run overwrites that vhost with no record of what it replaced.

## Behaviour

Two commands in a new `domains` group:

```
cloudfall domains apply <domain> [--issue-certificate] [--yes]
cloudfall domains observe <domain>
```

`<domain>` is a Domain's `metadata.id`. Both commands validate the project first, as
`deploy` does. Invalid config exits `CONFIG_INVALID`. A valid config with no Domain of that
id exits `PRECONDITION` with context code `lifecycle_domain_missing`, the way `deploy`
refuses an unknown component (`lifecycle_component_missing`). Both refusals happen before
the inventory render and before any SSH connection.

**domains apply**

- Runs `domains.yml` limited to the Domain's `proxy.server` and to that one Domain. No other host is contacted, and the proxy server renders no other Domain
- The play runs only the `cloudfall_nginx_site` role, so no baseline, firewall, time, access or service task runs. It is safe on a server whose baseline another project owns
- `origin.server` gets nothing, even when it differs from the proxy server: `domains.yml` has no origin-side task today, and no playbook reads the rendered `cloudfall_origin_domains` variable
- Without `--yes` it changes nothing and exits 0 with a plan, like `deploy`: `status: "plan"`, `action: "domains-apply"`, `domain`, `servers` (the proxy server), `wouldRun`, `instruction` and `effect: "would_update"`. The plan runs every controller-side check the run does, the DNS check included
- With `--yes` it answers `status: "ok"`, `action`, `domain`, `servers`, `receipt` (the receipt path) and `effect: "updated"`
- `--issue-certificate` checks DNS on the controller first, then runs the role with `cloudfall_domains_issue_certificates` true. The role's own sequence is the one `migrate` relies on: the vhost goes live over HTTP to serve the ACME challenge, certbot issues the certificate by webroot, the vhost is rendered again with TLS, and `certbot.timer` is enabled
- The DNS check covers `primaryName` and every alias. Each name must resolve to at least one address of the proxy server, computed as `migrate`'s `dns-verify` does (the server address, the names it resolves to, and `network.ipv4`). Otherwise the command exits `PRECONDITION` with context code `domains_dns_unverified`, naming each failing name and what it resolved to
- A Domain with `edge.mode: proxied` skips the DNS check, as `dns-verify` does, and the answer says `dnsCheck: "skipped-proxied"`. A checked Domain answers `dnsCheck: "verified"`. Without `--issue-certificate` there is no DNS check and no `dnsCheck` value
- `--issue-certificate` on a Domain with `tls.mode: disabled` exits `PRECONDITION` before any engine step
- A failed playbook run, such as a vhost that `nginx -t` rejects (the role rolls it back) or a failed issuance, exits `ENGINE_STEP_FAILED`
- Receipts go to `--deployments` (default `tmp/deployments`), the folder `migrate` and `converge_domains` use

**Receipt**

Every `domains.yml` run keeps writing one `DeploymentReceipt` per Domain at
`<deployments>/<domain>.json` (`config/schemas/v1/deployment-receipt.schema.json`). Its
`spec` gains an optional `previousConfigurationSha256`: the sha256 of the file at
`proxy.configurationPath` before this run rendered it, or `null` when no file was there. The
role already reads that file before rendering
(`engine/ansible/roles/cloudfall_nginx_site/tasks/apply_virtual_host.yml`), so it hashes it
there. The role writes the receipt, so runs from `migrate` and `converge_domains` record the
field too. A receipt without the field still loads.

**domains observe**

Read-only. It runs a new read-only playbook against the proxy server only, and writes
nothing to the server or to the project. It reports:

- `server`: the proxy server
- `vhost`: `path` (`proxy.configurationPath`), `present`, and `sha256` when present
- `receipt`: `configurationSha256` and `deployedAt` from `<deployments>/<domain>.json`, and `matches` saying whether the vhost on disk has that hash; `null` when there is no receipt
- `certificate`: `path` (`/etc/letsencrypt/live/<primaryName>/fullchain.pem`), `present`, and `notAfter` when present
- `certbotTimer`: `enabled` and `active`

It exits 0 whatever it finds: a missing vhost or an expired certificate is a reading, not an
error. An unreachable server exits `ENGINE_STEP_FAILED`. The playbook hands its reading back
through a controller-side file, as `backup.yml` does with its receipt. `services observe`
stays the public-network view of the route; `domains observe` is the server-side view.

**MCP**

`domains.apply` and `domains.observe` join `PROJECT_COMMANDS` in
`sdk/src/cloudfall/agent_server.py`, so `cloudfall mcp serve` serves them as tools generated
from the commands. The provided `converge_domains` tool (`_engine_tools` in
`sdk/src/cloudfall/app.py`) gains an optional `domain` string. Given, the tool runs on that
Domain's proxy server only and renders only that Domain, with `issue_certificates` working
as today (no DNS check). An unknown id exits `PRECONDITION`. Omitted, the tool behaves as
today.

Touches `sdk/src/cloudfall/app.py`, `sdk/src/cloudfall/commands.py`,
`sdk/src/cloudfall/agent_server.py`, `engine/ansible/playbooks/domains.yml`, a new read-only
playbook under `engine/ansible/playbooks/`, `engine/ansible/roles/cloudfall_nginx_site`,
`config/schemas/v1/deployment-receipt.schema.json`, `domains apply`, `domains observe` and
the `converge_domains` MCP tool.

## Acceptance criteria

- S-004-1: In a project with servers `a` and `b` and a Domain whose `proxy.server` is `a`, `cloudfall domains apply <d> --yes` runs `domains.yml` with only `a` in its play recap, and no task from `baseline.yml`, `services.yml` or the `cloudfall_firewall` role runs
- S-004-2: With Domains `d1` and `d2` both proxied on `a`, `domains apply d1 --yes` renders only `d1`'s `proxy.configurationPath`, and `d2`'s vhost file and receipt are unchanged
- S-004-3: Without `--yes`, `domains apply <d>` exits 0 with `status: "plan"` and `effect: "would_update"`, runs no `ansible-playbook`, and writes no receipt
- S-004-4: `domains apply nope` and `domains observe nope` in a valid project with no Domain `nope` exit `PRECONDITION` with context code `lifecycle_domain_missing`, and neither the inventory render nor `ansible-playbook` runs
- S-004-5: `domains apply <d>` in a project whose config fails validation exits `CONFIG_INVALID` before any engine step
- S-004-6: `--issue-certificate` on a `dns-only` Domain with an alias that resolves to no address of the proxy server exits `PRECONDITION` with context code `domains_dns_unverified` naming that alias, with and without `--yes`, and runs no `ansible-playbook`
- S-004-7: `--issue-certificate` on a `proxied` Domain runs no DNS check and answers `dnsCheck: "skipped-proxied"`
- S-004-8: `--issue-certificate` on a Domain with `tls.mode: disabled` exits `PRECONDITION` before any engine step
- S-004-9: `domains apply <d> --issue-certificate --yes` on a fresh `dns-only` Domain whose names resolve to the proxy server passes `cloudfall_domains_issue_certificates: true` to `domains.yml`, ends with `/etc/letsencrypt/live/<primaryName>/fullchain.pem` present and `certbot.timer` enabled and active, answers `dnsCheck: "verified"`, and writes `<deployments>/<d>.json`
- S-004-10: A second `domains apply <d> --issue-certificate --yes` skips the `Issue the declared certificate` task, and its receipt has `configurationSha256` equal to `previousConfigurationSha256`
- S-004-11: A `domains.yml` run on a proxy server with a hand-written file at `proxy.configurationPath` writes a receipt whose `previousConfigurationSha256` is that file's sha256, and a run with no file there writes `null`
- S-004-12: A `DeploymentReceipt` without `previousConfigurationSha256` still loads in `services status`
- S-004-13: A vhost that `nginx -t` rejects makes `domains apply <d> --yes` exit `ENGINE_STEP_FAILED`
- S-004-14: `domains observe <d>` reports `vhost.present`, `vhost.sha256`, `certificate.notAfter`, `certbotTimer.enabled` and `certbotTimer.active` as read on the proxy server, exits 0 when the vhost file is missing, and changes no file on the server or in the project
- S-004-15: `domains observe <d>` reports `receipt.matches: false` once the vhost file was edited after the last receipt, `true` while it is unchanged, and `receipt: null` when no receipt exists
- S-004-16: `cloudfall mcp serve` on a project lists a tool for `domains apply` and one for `domains observe`
- S-004-17: The `converge_domains` tool called with `domain: <d>` runs only on `d`'s proxy server and renders only `d`, called without `domain` runs on every server in `cloudfall_servers` as before, and called with an unknown `domain` exits `PRECONDITION`

## Out of scope

- Origin-side tasks: nothing in `domains.yml` acts on `origin.server` today. A spec that adds an origin allowlist or a private upstream can extend `domains apply` to the origin then
- Opening ports 80 and 443: the firewall belongs to the baseline, which this command must not touch on a lent server. If the firewall blocks them, issuance fails with `ENGINE_STEP_FAILED`
- Removing a Domain's vhost or certificate: deletion needs its own plan and refusal rules
- A `--server` flag or several domains per call: one Domain has one proxy server, and a loop over ids covers several
- Storing `domains observe` readings as evidence files: `services observe` already stores the public view, and the server view can join it later if the dashboard needs it
- Compatibility of new receipts with an older `cloudfall`: the receipt schema sets `additionalProperties: false`, so an older `services status` refuses a receipt that carries `previousConfigurationSha256`. Upgrading `cloudfall` first is the path, as for any new receipt field
- `migrate --only domains-http,domains-tls`: a step filter on a resumable plan is easy to get wrong and still reads as a migration

## Decisions relied on

- none

## Issues

- cloudfall-dev/cloudfall#116: S-004-2, S-004-11, S-004-12, S-004-13, S-004-17
- cloudfall-dev/cloudfall#117: S-004-1, S-004-3, S-004-5, S-004-6, S-004-7, S-004-8, S-004-9, S-004-10
- cloudfall-dev/cloudfall#118: S-004-4, S-004-14, S-004-15, S-004-16

## Verification

No criterion has run against a live Debian host yet. The SDK side ran through recording runners and through `ansible-playbook` against local connections (every server a `local` host, `cloudfall_nginx_site` swapped for a recording role); the role's certificate and receipt tasks ran on localhost or had their `when` conditions evaluated with Ansible's templating; `domains-observe.yml` ran for real on localhost with stand-ins for `openssl` and `systemctl`. Real Nginx (`nginx -t`, the reload), real certbot issuance by webroot, a real `certbot.timer`, `openssl` reading a real certificate, and SSH to a lent server are unexercised.

- S-004-1: `test_s004_1_only_the_proxy_server_runs_domains_yml` ran `domains.yml` with a recording role on two local hosts and found only `h2` in the play recap and only the domains tasks; `test_s004_1_domains_yml_runs_no_baseline_firewall_or_service_role` found `cloudfall_nginx_site` the only role the playbook can reach
- S-004-2: `test_s004_2_one_domain_id_renders_only_that_domain` and `test_s004_2_a_host_without_the_domain_renders_nothing` evaluated the render loop with Ansible's templating; `test_s004_2_one_domain_runs_on_its_proxy_and_names_only_it` read the `--limit` and extra vars of the recorded run. A second Domain's files on a real server were not checked
- S-004-3: `test_s004_3_without_yes_the_plan_runs_nothing` (CLI) and `test_s004_3_the_plan_starts_no_engine_step`: no step started and no receipt
- S-004-4: `test_s004_4_apply_refuses_an_unknown_domain_before_any_engine_step`, `test_s004_4_observe_refuses_an_unknown_domain_before_any_engine_step` (CLI, no `tmp/` created) and `test_s004_4_observe_starts_no_step_for_an_unknown_domain`
- S-004-5: `test_s004_5_an_invalid_config_exits_before_any_engine_step`, with and without `--yes`
- S-004-6: `test_s004_6_an_alias_elsewhere_is_refused_naming_it` and `test_s004_6_a_name_that_resolves_to_nothing_is_refused`, with a stand-in resolver
- S-004-7: `test_s004_7_a_proxied_domain_skips_the_dns_check` (CLI) and `test_s004_7_a_proxied_domain_asks_no_resolver`
- S-004-8: `test_s004_8_tls_disabled_refuses_issue_certificate` and `test_s004_8_tls_disabled_is_refused_before_dns`
- S-004-9: `test_s004_9_issue_certificate_passes_the_flag_to_domains_yml` (the extra var and the receipt path) and `test_s004_1_only_the_proxy_server_runs_domains_yml` (the flag reached the recording role); `test_s004_9_a_fresh_domain_issues_and_enables_the_renewal_timer` evaluated the issuance and timer tasks' conditions only. No certificate was issued
- S-004-10: `test_s004_10_a_second_run_skips_issuance` (task conditions) and `test_s004_10_a_second_run_records_the_vhost_it_rendered_before` (the receipt tasks ran twice on localhost)
- S-004-11: `test_s004_11_the_receipt_keeps_the_hash_of_a_hand_written_vhost`, `test_s004_11_a_run_with_no_vhost_there_records_null`, `test_s004_11_a_vhost_linked_into_place_is_hashed_through_the_link` and `test_s004_11_the_replaced_vhost_is_hashed_before_the_first_render`, the receipt tasks on localhost with a stand-in render
- S-004-12: `test_s004_12_a_receipt_with_or_without_the_previous_hash_loads`
- S-004-13: `test_s004_13_a_rejected_vhost_fails_the_run_as_an_engine_step`, with a failing recorded playbook step; a real `nginx -t` rejection was not run
- S-004-14: `test_s004_14_observe_reads_the_proxy_server_and_changes_nothing` and `test_s004_14_a_missing_vhost_is_a_reading_not_an_error` ran the real `domains-observe.yml` on local hosts (only `h2` in the recap; project and server files unchanged by content and modification time); `test_s004_14_the_playbook_holds_only_read_tasks` holds every server task to `stat`, `set_fact`, `assert` or a command with `changed_when: false`; `test_s004_14_the_cli_answers_snake_case_and_leaves_the_project_as_it_was` ran the CLI with a stand-in playbook and found the project unchanged, the inventory rendered outside it
- S-004-15: `test_s004_15_receipt_matches_until_the_vhost_is_edited`, `test_s004_15_no_receipt_reads_as_null` and `test_s004_15_a_removed_vhost_does_not_match_its_receipt` on local hosts, and `test_s004_15_the_cli_answers_the_receipt_comparison`
- S-004-16: `test_s004_16_a_project_serves_domains_apply_and_observe` over the real stdio transport: both tools listed, `domains_apply` without `yes` answered the plan
- S-004-17: `test_s004_17_the_tool_takes_a_domain_and_refuses_an_unknown_one` over the real stdio transport, `test_s004_17_without_a_domain_the_run_is_as_before`, `test_s004_17_an_unknown_domain_is_refused_before_any_step`, `test_s004_17_a_domain_that_is_no_resource_id_is_refused` and `test_s004_17_without_a_domain_id_every_domain_on_the_host_renders`
