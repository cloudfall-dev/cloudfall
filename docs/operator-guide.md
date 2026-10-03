# Operator guide

The operator is the always-on half of Cloudfall and the part that writes
the record: a management-host process that watches declared alerts and
audited drift, writes evidence-backed remediation proposals, executes them
only after an approval it does not grant itself, and verifies the result
before calling it done. It is deliberately deterministic — it never invents
actions, only maps declared triggers onto the engine entry points you
already use. The deciding is left to you or to your agent; the operator's
job is that every decision is gated, verified and on the record.

## Prerequisites

- A deployed logging stack with an `alerting` block (see the
  [logging service guide](logging-service-guide.md)) and at least one
  declared `AlertRule`
- The gateway's read-only alerts route, included automatically when the
  backend role runs: `GET /api/v1/alerts` over the same mTLS gateway that
  receives logs and metrics
- A client certificate for the management host, signed by the same CA that
  validates collector certificates (`collectors.clientTls`); the operator is
  just another mTLS client of the gateway

## Watching

One pass (fetch alerts, propose, exit):

```sh
uv run cloudfall operator run \
  --gateway-ca certs/ca.crt \
  --gateway-cert certs/operator.crt \
  --gateway-key certs/operator.key
```

Always-on with audited drift checks every 15 minutes:

```sh
uv run cloudfall operator run \
  --gateway-ca certs/ca.crt \
  --gateway-cert certs/operator.crt \
  --gateway-key certs/operator.key \
  --interval 30 --drift-interval 900
```

The alerts endpoint is derived from the declared gateway
(`https://<serverName>:<port>/api/v1/alerts`); pass `--gateway-url` to
override. Drift checks run the read-only `inspect.yml` playbook, refresh
observations under `--observed`, and audit them; drifted checks become
proposals split by remediation: service checks propose `services.yml`,
everything else proposes `baseline.yml`.

Each pass is one JSON line on stdout, written as the pass ends:
`data.pass` says which pass it was (`alerts`, `drift`, `autonomy`) and
`data.effect` what it did (`created` when it wrote proposals, `updated` when
it ran licensed ones, `noop` otherwise). A one-pass run ends with a closing
line carrying `meta.end`; stopping a watcher (Ctrl-C or SIGTERM) ends it
with a `CANCELLED` line and exit 130 or 143.

Every proposal is a schema-validated receipt in `--proposals`
(default `tmp/operator/proposals`): the trigger evidence (alert labels or
drifted checks), a diagnosis, the exact operation, and later its outcome.
An open or failed proposal blocks duplicates for the same trigger — a
failed remediation deliberately stays visible until a human looks at it.

## The record

The proposal store is the audit log. Each receipt answers "why did the
operator do that" without anyone writing a note:

- **What it saw**: the alert labels or the drifted checks that triggered it
- **What it proposed**: the diagnosis and the exact engine operation
- **Who approved**: a human through `operator approve`, an agent through
  the `operator_approve` tool, or a declared `OperatorPolicy`
  (`approval.mode: autonomous` with the policy id)
- **What happened**: `verified` or `failed`, with timestamps, from the
  verify step below, never from the playbook's exit code

`operator list` and `operator show` read the record; the dashboard renders
it. Autonomy in the next section is computed from it. The
[brownfield design](brownfield-design.md) generalises this entry to every
declared operation as a decision record with the fleet snapshot and the
check-mode diff, and `cloudfall why` queries those records by host,
operation or time window. The operator's receipts here are the older
form of the same entry and are not yet read by `cloudfall why`.

To run persistently, wrap the command in a systemd service on the
management host:

```ini
[Unit]
Description=Cloudfall operator (propose mode)
After=network-online.target

[Service]
WorkingDirectory=/opt/my-project
Environment=CLOUDFALL_PROJECT=/opt/my-project
ExecStart=/usr/local/bin/uv run cloudfall operator run \
  --gateway-ca certs/ca.crt --gateway-cert certs/operator.crt \
  --gateway-key certs/operator.key --interval 30 --drift-interval 900
Restart=on-failure
# `systemctl stop` sends SIGTERM, which the operator answers with exit 143.
SuccessExitStatus=143

[Install]
WantedBy=multi-user.target
```

## Reviewing and approving

```sh
uv run cloudfall operator list
uv run cloudfall operator show op-20260910t150000-abcdef1234567890
uv run cloudfall operator approve op-... \
  --gateway-ca certs/ca.crt --gateway-cert certs/operator.crt \
  --gateway-key certs/operator.key
```

Approval executes the proposal's engine playbook and then verifies the
trigger actually resolved: an alert-triggered proposal must see its alert
stop firing, a drift-triggered proposal must see its drifted checks audit
compliant again (`--verify-timeout`, default 180 s). The receipt records
`verified` or `failed` with timestamps either way — the operator never
reports success it cannot prove. A playbook that ran is not an outcome; the
verify step is. Gateway TLS flags are only needed when approving
alert-triggered proposals; drift approvals verify through the audit.

## Graduated autonomy

Declaring an `OperatorPolicy` licenses the daemon to execute routine
proposals without asking — that declaration *is* the consent, and its
absence means propose-only forever:

```yaml
apiVersion: cloudfall/v1
kind: OperatorPolicy
metadata:
  id: production-operator
spec:
  environment: production
  autonomy:
    operations:
      - kind: converge-services
        requiredVerifiedRuns: 3
      - kind: converge-baseline
        requiredVerifiedRuns: 5
    maxAutonomousPerHour: 4
    quietHours:
      start: "01:00"
      end: "05:00"
```

Autonomy is earned, bounded, and revocable:

- **Earned by receipts**: an operation kind executes autonomously only after
  `requiredVerifiedRuns` verified receipts for that kind exist in the
  proposal store — human-approved runs build the trust history
- **Suspended on failure**: if the most recent receipt for a kind failed,
  autonomy for that kind is withheld until a human-approved run verifies
  again
- **Rate-limited and time-bounded**: at most `maxAutonomousPerHour`
  autonomous executions, and none during declared quiet hours; withheld
  proposals stay open for normal human approval
- **Structurally confined**: the policy schema can only grant the
  convergence operations. DNS cutover, data deletion, and database
  promotion are not grantable — confirm forever, by construction

Every autonomous execution is receipted with `approval.mode: autonomous`
and the licensing policy's id, verified exactly like a human-approved run,
and visible in `operator list`. When a policy is declared, the running
daemon adds an autonomy pass after each watch pass and reports which
proposals it executed and which it withheld, with reasons.

## Through an agent

`cloudfall mcp serve` exposes the record and the approval; start it with
the gateway material so an alert-triggered approval can verify:

```sh
uv run cloudfall mcp serve \
  --gateway-ca certs/ca.crt --gateway-cert certs/operator.crt \
  --gateway-key certs/operator.key
```

- `operator_list` and `operator_show` (read-only): the receipts with
  trigger, diagnosis, and outcome
- `operator_approve`: executes one proposal and verifies its trigger
  resolved, in one call, as `cloudfall operator approve` does

The watch loop itself is not a tool: it runs until stopped, as the service
above.

The agent is the brain here and the operator is the record. The agent can
watch, read, relay the diagnosis and decide to approve; the tools are
annotated so the client gates the mutating one with its own permission
mode; the server adds no confirm step of its own. Whichever
way a proposal is approved, the receipt is the same, so an agent-approved
remediation is as explainable afterwards as a human-approved one.
