# S-015: Alert channels shared by hosts and the logging stack

status: approved
serves: G-1, G-2

## Problem

A server has two sources of alerts that a person must see, and Cloudfall delivers neither on its
own:

- **Host events.** mdadm reports a failed or degraded array, smartd a failing disk. Debian sends
  both to local `root` mail, which nobody reads on a host without an MTA. A project that wants them
  writes its own sender and wires mdadm's `PROGRAM` and smartd's `-M exec` to it by hand
- **Alert rules.** A `LoggingStack`'s Alertmanager delivers to a `webhook` or `email` receiver
  only. A team that already gets host events in a chat has to run a bridge from a webhook to that
  chat, or read alerts in two places

Teams want one channel for both, declared once. The first channel people ask for is a Telegram
chat with a bot: no mail server, no inbound port, delivered to a phone.

## Behaviour

### The channel

A new kind, `AlertChannel` (`alert-channels/`), declares where alerts go:

```yaml
apiVersion: cloudfall/v1
kind: AlertChannel
metadata:
  id: operations
spec:
  telegram:
    chatId: "145629422"
```

- `telegram.chatId` is the chat's id: digits, optionally led by `-` for a group. It is not a
  secret
- The bot token is a secret requirement (S-011) of every server that delivers to the channel:
  source `input`, kept in `secrets/<environment>/alert-channels/<id>.env` as
  `TELEGRAM_BOT_TOKEN`, delivered to `/etc/cloudfall/alerting/<id>/bot-token` (`0640`, `root`
  and the group of the units that read it). A person sets it once with `cloudfall secrets input
  telegram-bot-token --owner <id>`
- Telegram is the only channel type in this spec; the kind leaves room for others

### Host events

A `Baseline` names the channel its hosts report to:

```yaml
spec:
  alerting:
    channel: operations
```

On a server with that baseline, the bundled `baseline` playbook:

- installs `/usr/local/sbin/cloudfall-alert`, which sends one message to the channel: a plain text
  argument, an mdadm event (`EVENT DEVICE [COMPONENT]`, with that array's `/proc/mdstat` lines), or
  a smartd event (from its `SMARTD_*` environment). Every message starts with the host's FQDN. The
  token travels to `curl` on stdin, never in `argv` or a log line; a failed send is logged to the
  journal and exits non-zero
- writes the chat id to `/etc/cloudfall/alerting/<id>/chat-id`
- when mdadm is installed, sets `PROGRAM /usr/local/sbin/cloudfall-alert` in `mdadm.conf`; when
  smartd is installed, adds `-M exec /usr/local/sbin/cloudfall-alert` to its `DEVICESCAN` line;
  both daemons are reloaded when their file changes
- leaves a host whose baseline names no channel exactly as today

The token arrives through the S-011 secrets provider: `baseline` stops before any change when the
token is not in the store, with exit 4 and `INPUT_REQUIRED`, as `logging deploy` does.

### Alert rules

A `LoggingStack` receiver may name the channel instead of a webhook or an email:

```yaml
alerting:
  receivers:
    - id: operations
      channel: operations
```

Alertmanager renders it as a native `telegram_configs` entry with `bot_token_file` pointing at
the delivered token and `chat_id` from the channel, so no bridge runs. `logging deploy` delivers
the token to the backend with the stack's other secrets. Alertmanager's package must support
`bot_token_file` (0.26 or later); the role checks its version before writing the configuration and
stops naming the version when it is older.

### Test and audit

- `cloudfall alerts test <server>` sends one message through the server's channel with
  `cloudfall-alert`, naming the server and the time; without `--yes` it shows the plan. It is the
  verify step of a channel
- `inspect` records whether `cloudfall-alert` is installed, whether the token and chat-id files
  exist (never their content), and the mdadm `PROGRAM` and smartd `DEVICESCAN` lines; `audit`
  adds `alerting.channel`: compliant when all four match the baseline's channel

## Acceptance criteria

- S-015-1: An `AlertChannel` with a numeric `telegram.chatId` passes `config validate`; a chat id
  with other characters, a channel with no type, or a `Baseline` or receiver naming a channel that
  is not declared fails it with `CONFIG_INVALID`
- S-015-2: `requirements show` lists the `telegram-bot-token` secret as `needs-input` on every
  server whose baseline names the channel, and on the logging backend when a receiver names it;
  after `secrets input` it is `met`
- S-015-3: `baseline` on a host whose baseline names a channel, with the token stored, installs
  `cloudfall-alert`, the token (`0640`) and chat-id files, sets mdadm's `PROGRAM` and smartd's
  `-M exec`, and a second run changes nothing; without the token stored it exits 4 with
  `INPUT_REQUIRED` and changes nothing
- S-015-4: `cloudfall alerts test <server> --yes` delivers one message to the chat whose text starts
  with the server's FQDN; `mdadm --monitor --scan --test --oneshot` delivers one message per array
- S-015-5: A `LoggingStack` receiver with `channel` renders `telegram_configs` with `bot_token_file`
  and the chat id, and an alert rule that fires reaches the chat; an Alertmanager older than 0.26
  stops the role before its configuration is written
- S-015-6: The bot token appears in no `argv`, journal line, rendered inventory, command answer,
  receipt or observation
- S-015-7: `audit` reports `alerting.channel` compliant after S-015-3, and drift when the sender,
  the token file or mdadm's `PROGRAM` is missing
- S-015-8: A host whose baseline names no channel converges and audits exactly as before

## Out of scope

- Channel types other than Telegram (Slack, ntfy, a webhook as a host channel): later specs
- Alerts from LVM's `dmeventd` and from a filesystem filling up: they come from `AlertRule`s on
  the logging stack's metrics (`node_filesystem_avail_bytes`), which this spec delivers
- Rate limiting, grouping and silences of host events: Alertmanager does this for alert rules;
  host events are rare and sent as they come
- Moving a project's own sender (such as one in a project playbook) onto this: the project removes
  its copy once `audit` is compliant

## Decisions relied on

- none

## Issues

Feature issue cloudfall-dev/cloudfall#274, built in four slices; cloudfall-dev/cloudfall#281
repairs the `cloudfall_alerting` role's smartd line and handlers (S-015-3).

- cloudfall-dev/cloudfall#276: S-015-1, S-015-2
- cloudfall-dev/cloudfall#277: S-015-3
- cloudfall-dev/cloudfall#278: S-015-5
- cloudfall-dev/cloudfall#279: S-015-4, S-015-6, S-015-7, S-015-8

## Verification

Not built yet: every criterion has a test on the controller, but the parts below marked "not
yet verified live" wait on a Debian host with a real Telegram bot, a real mdadm array and the
journal, and S-015-3 on the repair in #281. Per criterion:

- S-015-1: unit tests, `test_s015_1_*` in `sdk/tests/test_s015_channels.py`: a numeric and a
  group chat id validate; other characters, a trailing newline, and a `Baseline` or receiver
  naming an undeclared channel are refused
- S-015-2: unit tests, `test_s015_2_*` in `sdk/tests/test_s015_channels.py`: the token on
  every server of the baseline, on the backend for a receiver alone, once per server;
  `test_s015_2_after_secrets_input_the_token_is_met` in `sdk/tests/test_s015_whole_spec.py`:
  `met` on every server once the token is stored
- S-015-3: `test_s015_3_a_missing_token_stops_before_any_server` and
  `test_s015_3_the_token_reaches_each_host_in_a_private_file` (the CLI stops with exit 4 and
  `INPUT_REQUIRED` before any playbook, and stages a `0600` file); the role run for real
  against scratch files, idempotent, in `test_s015_3_the_role_installs_the_sender_and_wires_both_sources`
  and `test_s015_3_without_a_token_the_role_stops`. Not yet verified live: mdadm and smartd
  reloads on a Debian host, after #281
- S-015-4: `test_s015_4_the_plan_names_the_channel_and_sends_nothing`,
  `test_s015_4_the_message_goes_to_that_server_only` (`alerts-test.yml`, limited to the
  server) and `test_s015_4_a_server_without_a_channel_is_refused`; the message's FQDN prefix
  in `test_s015_6_the_sender_keeps_the_token_out_of_argv`. Not yet verified live: a message
  reaching a real chat, and `mdadm --monitor --scan --test --oneshot` sending one per array
- S-015-5: unit tests, `test_s015_5_*` in `sdk/tests/test_s015_alertmanager.py`: the native
  `telegram_configs` with `bot_token_file` and the chat id, the token staged for Alertmanager
  or the deploy stopped, the group made before the token lands, and an Alertmanager older
  than 0.26 refused. Not yet verified live: a firing rule reaching the chat
- S-015-6: `test_s015_6_the_token_reaches_no_argv_inventory_answer_or_record` stores a
  sentinel token and runs `requirements show`, `baseline converge`, `logging deploy` and
  `alerts test`, each as a plan and with `--yes`: the sentinel is in no `ansible-playbook`
  argv, no rendered inventory, no command answer, and no file under the project but the
  secret store; `test_s015_6_the_token_is_handed_over_staged_and_then_removed` shows the
  staged file is gone after the run; `test_s015_6_the_sender_keeps_the_token_out_of_argv`
  shows the sender hands it to `curl` on stdin and logs no line with it; the inspect test
  marked `proves: S-015-6` shows the observation holds only whether the token file exists.
  Not yet verified live: the journal of a real host after a send and a failed send
- S-015-7: unit tests, `test_s015_7_*` in `sdk/tests/test_s015_test_and_audit.py`: compliant
  when the sender, the token, the chat id and the installed daemons' lines are there, drift
  for each one missing or pointing elsewhere, unknown without evidence. Not yet verified
  live: `audit` on a converged host
- S-015-8: `test_s015_8_a_host_without_a_channel_converges_with_no_channel` (no secret read,
  an empty channel map to `baseline.yml`), `test_s015_8_a_channel_adds_one_host_variable_and_nothing_else`
  (a no-channel project renders no alerting variable; a channel adds only its own),
  `test_s015_8_baseline_skips_the_alerting_role_without_a_channel` (the playbook's task,
  run, skips the role) and `test_s015_8_a_host_without_a_channel_audits_as_before` (no
  `alerting.channel` check, every check compliant on the compliant fixture)

Landed, slice by slice, checked by unit tests on the controller:

- The `AlertChannel` kind (`alert-channels/`, Telegram chat id), a `Baseline`'s
  `alerting.channel` and a `LoggingStack` receiver's `channel`, refused by `config validate`
  when the channel is not declared (`alert_channel_missing`): S-015-1
- The `telegram-bot-token` input secret, owned by the channel, on every server whose baseline
  names it and on the logging backend when a receiver does, once per server; `requirements
  show` lists it: S-015-2
- Host events: `baseline.yml` runs the new `cloudfall_alerting` role on every server whose
  baseline names a channel: the token (`0640`), the chat id, `/usr/local/sbin/cloudfall-alert`,
  mdadm's `PROGRAM` and smartd's `DEVICESCAN ... -M exec`, idempotent; it stops when the token
  was neither delivered nor already on the host. The new `cloudfall baseline converge` (plan
  without `--yes`) and the `converge_baseline` MCP tool stage each host's token in a `0600` file
  from the store and stop with exit 4 and `INPUT_REQUIRED` when it is missing, before any
  server: S-015-3. The sender sends the token to `curl` on stdin; tests with stand-in `curl`,
  `logger` and `hostname` show it in no argv and no log line: part of S-015-6. The role ran for
  real on the controller against scratch files; mdadm and smartd reloads and a real send wait on
  a Debian host
- Alert rules: a receiver with `channel` renders Alertmanager's native `telegram_configs`
  (`bot_token_file`, numeric `chat_id`) from the inventory's `cloudfall_logging_alert_channels`.
  `logging deploy` stages the channel's token for Alertmanager at
  `/etc/cloudfall/logging/alert-channel-<id>-bot-token` (`0640`, group `prometheus`), apart from
  the host's copy, so the baseline and the stack never fight over its group; a token not in the
  store stops the deploy with `INPUT_REQUIRED`. The backend role checks the Alertmanager version
  it would install (the pin, else `apt-cache policy`'s candidate) is 0.26 or later before writing
  its configuration: S-015-5 on the controller; a real alert reaching the chat waits on a Debian
  host
- Test and audit: `cloudfall alerts test <server>` (plan without `--yes`) runs `alerts-test.yml`
  limited to that server, which sends one message naming the server, the channel and the time
  through the installed sender; a server whose baseline names no channel is refused. `inspect`
  records the channel's files (the sender, whether the token and chat id exist, never the
  token) and mdadm's `PROGRAM` and smartd's `DEVICESCAN` lines; `audit` adds `alerting.channel`,
  compliant when all are there and the installed daemons point at the sender, drift otherwise,
  unknown without evidence: S-015-4 on the controller, S-015-7. A real message reaching the chat
  waits on a Debian host

