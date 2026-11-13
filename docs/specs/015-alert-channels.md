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

## Verification
