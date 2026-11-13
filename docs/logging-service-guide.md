# Deploy the first Cloudfall observability service

Status: implemented parallel-migration slice; no production `LoggingStack` is
declared yet.

This workflow installs a single-node Loki and Prometheus backend,
loopback-only Grafana, an mTLS-only Nginx ingestion gateway, and Grafana
Alloy collectors that ship both logs and host metrics. It never stops,
disables, reconfigures, or removes Filebeat, Metricbeat, Elasticsearch,
Kibana, or Logstash.

Host metrics use Alloy's embedded `prometheus.exporter.unix`, so collectors
open no additional listening port; metrics are pushed through the same mTLS
gateway to Prometheus's remote-write receiver on the backend host.

This stack is where the operator's signals come from. Cloudfall is the
operator's record for self-hosted servers run by an AI agent, and the record
needs evidence to act on: a declared `AlertRule` firing here is what
triggers a proposal, the same gateway's read-only alerts route is what the
operator and the agent read, and an alert that stops firing is how an
approved remediation is verified (see the [operator
guide](operator-guide.md)). Without this stack the operator has nothing to
watch and nothing to verify against.

## Safety boundary

The v1 `LoggingStack` contract intentionally permits only:

- migration mode `parallel` with `preserveLegacyAgents: true` beside a legacy
  pipeline, or `none` on a host that has no legacy agents (`preserveLegacyAgents`
  is then rejected);
- Loki, Prometheus, and Grafana bound to `127.0.0.1`;
- a gateway that exposes only `/loki/api/v1/push` and `/api/v1/write` and
  requires a validated client certificate;
- TSDB schema v13 with bounded filesystem retention for logs, bounded
  Prometheus retention for metrics, and distinct backend listener ports; and
- pinned Debian package versions from Grafana's stable APT repository, with
  Prometheus installed from Debian (optionally pinned via
  `software.prometheusPackageVersion`).

The gateway does not expose Loki's or Prometheus's query, administration,
deletion, readiness, or self-metrics APIs. Grafana remains reachable only
through the backend host, for example with an SSH tunnel. This first slice is a proving deployment, not a
high-availability logging cluster. Filesystem storage must be on mirrored
storage and included in capacity monitoring and backup policy.

## 1. Prepare placement and names

Add the dedicated logging host as a `Server` and give it an accurate
`Baseline`. Do not use the capacity-constrained public proxy or a host with
an unresolved storage finding.

Choose an internal gateway name, such as `logs.internal.example`, whose A/AAAA
record points to the logging host. The server certificate must contain that
name as a DNS subject alternative name.

Before pinning versions, confirm that all packages exist for the target Debian
release:

```console
apt-cache policy loki grafana alloy
```

Use the complete APT version strings in state. Cloudfall deliberately fails rather
than silently replacing an unavailable pin with the newest package: each role
checks its pins with `apt-cache madison` before it installs or configures
anything, and names the versions the host offers when a pin is missing.

## 2. Secrets and mTLS files

Cloudfall state contains paths only, and `cloudfall logging deploy` provides
every file at those paths (S-011):

| Backend host | Collector hosts | Provided by |
| --- | --- | --- |
| `/etc/cloudfall/logging/server.crt` | `/etc/cloudfall/logging/client.crt` | the project CA, signed for the gateway name or the collector's hostname |
| `/etc/cloudfall/logging/server.key` | `/etc/cloudfall/logging/client.key` | made on the host; it never leaves it |
| `/etc/cloudfall/logging/client-ca.crt` | `/etc/cloudfall/logging/ca.crt` | the project CA's certificate |
| `/etc/cloudfall/logging/grafana-admin-password` | | generated into `secrets/` |
| each receiver's `authPasswordFile` | | `cloudfall secrets input` |

The project CA is one per environment, created on first use, its key
sops-encrypted in `secrets/<environment>/pki/ca.env`. Each host makes its own
key and a signing request; the CA signs only the request's public key, for
the names and usage the stack implies, for 90 days, and a deploy renews a
certificate within 30 days of its expiry.

Files provided by hand keep working: a secret already on the host is kept,
and a certificate another CA signed is left as it is with its CA file. Do not
commit certificates, keys, or passwords to the repository yourself.

Private keys and the password must not be world-readable. The roles verify all
files before installing packages, then apply the minimum service-readable
ownership and modes.

The gateway port needs no firewall entry by hand: `logging deploy` opens it on
the backend to each remote collector's addresses only, so a collector behind a
default-deny firewall needs an IP, as its `address` or a `network` block.

## 3. Declare the stack and sources

Copy the synthetic
[`operations` example](../config/examples/logging-stacks/operations.yaml) into
your project's `logging-stacks/`, then replace every example host, name,
version, path, and retention value.

Declare every journald collector host and every application file glob. A file
source can attach only the bounded labels `environment`, `server`, `source`,
`job`, `application`, and `component`; request IDs, user IDs, URLs, and other
unbounded values are not part of the state contract.

Alloy runs as the `alloy` account. Grant read and traversal access only to the
declared application paths. For example, after reviewing the path and owner:

```console
setfacl -m u:alloy:rx /var/log/crm /var/log/crm/backend
setfacl -m u:alloy:r /var/log/crm/backend/current.log
setfacl -d -m u:alloy:r /var/log/crm/backend
```

Do not recursively broaden unrelated `/var/log` permissions.

## 4. Validate and inspect the plan

```console
uv run cloudfall config validate
uv run cloudfall inventory show
uv run cloudfall requirements show
uv run cloudfall-engine inventory render \
  --output tmp/ansible-inventory.json
uv run ansible-inventory --inventory tmp/ansible-inventory.json \
  --graph cloudfall_logging_backends
uv run ansible-inventory --inventory tmp/ansible-inventory.json \
  --graph cloudfall_logging_collectors
```

`requirements show` lists what the stack needs from each server: the Loki and
Prometheus volumes, the gateway port open to remote collectors, the Grafana and
SMTP passwords, the gateway and collector certificates, the gateway name and the
package pins. Secrets carry their state, and `data.missing` names the ones a
person must supply. `uv run cloudfall secrets generate` stores the Grafana
password; `uv run cloudfall secrets input smtp-password-<receiver> --owner
<stack>` stores an SMTP password typed at a terminal. Both keep the value
sops-encrypted under `secrets/`, and `cloudfall logging deploy` copies it to
its host path; it makes the keys, requests and certificates as section 2 says.

Validation rejects missing servers, environment mismatches, duplicate stacks,
file sources outside component placement, secret paths outside
`/etc/cloudfall/logging`, and any attempt to disable the parallel-migration guard
(`parallel` without `preserveLegacyAgents: true`).

## 5. Check and deploy

Without `--yes` the command is the plan: every requirement with its state,
the secrets it would generate, and `data.missing`, the secrets a person must
supply first. With `--yes` it generates the missing generated secrets, stops
with exit 4 and `error.context.code` `INPUT_REQUIRED` while an input secret is
missing, then runs `logging.yml` with each stored secret staged in a `0600`
file on the controller. A secret file already on the host is kept, so one
provided by hand is never overwritten:

```console
uv run cloudfall logging deploy
uv run cloudfall logging deploy --yes
```

`task logging:check` and `task logging:deploy` still run the playbook directly,
without what Cloudfall provides; they need every file of section 2 on the host.

The deployment order is backend first and collectors second, one host at a
time. Loki, Prometheus, and Alloy configuration are validated with their
native binaries; Nginx is checked with `nginx -t`; services must then pass
readiness checks.

## 6. Verify end to end

A deploy that ran is not a stack that works; this section is the verify
step, and the deployment is not done until it passes. From a collector,
verify the mTLS gateway without exposing credentials in the command history
through environment variables:

```console
curl --fail --silent --show-error \
  --cacert "$CLOUDFALL_LOGGING_CA" \
  --cert "$CLOUDFALL_LOGGING_CERT" \
  --key "$CLOUDFALL_LOGGING_KEY" \
  "https://logs.internal.example:3101/loki/api/v1/push" \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"streams":[]}'
```

Open Grafana through an SSH tunnel:

```console
ssh -L 3000:127.0.0.1:3000 cloudfall@logging-host
```

Then inspect `http://127.0.0.1:3000`, confirm the provisioned `Cloudfall Loki` data
source, and query each declared `server`, `source`, `job`, `application`, and
`component`. Compare counts, timestamps, multiline behavior, ingestion delay,
and alert coverage with the legacy Elastic path for the full proving window.

Confirm host metrics through the `Cloudfall Prometheus` data source: every
declared collector must report `node_cpu_seconds_total`,
`node_filesystem_avail_bytes`, and `node_memory_MemAvailable_bytes` series
labeled with its `server` and `environment` within two collection intervals.

## What the stack feeds

Once verified, the stack becomes an input to everything else Cloudfall
records:

- **Alert rules**: declare `AlertRule` resources (see the
  [`postgresql-down` example](../config/examples/alert-rules/postgresql-down.yaml));
  they render into the backend and fire through the gateway's
  `GET /api/v1/alerts` route, which requires the same client certificate
  as ingestion
- **The operator**: watches that route, writes a proposal receipt per
  firing alert with the alert labels as its trigger evidence, and verifies
  a remediation by seeing the alert stop
- **The agent**: reads what the operator recorded from that route through
  `cloudfall mcp serve` (`operator_list`, `operator_show`); it never queries
  Loki or Prometheus directly, and the gateway does not let it
- **The audit**: the running units, pinned versions and listener bindings
  declared here are checked by `cloudfall audit` like any other service,
  so the stack that produces the evidence is itself evidence

## Roll back the pilot

Because Filebeat remains active, collector rollback is limited to Alloy:

```console
systemctl disable --now alloy.service
```

Do not delete Loki data or remove the legacy logging path during the proving
window. Diagnose and correct the declared source or role, rerun validation, and
deploy again. Legacy retirement remains a separate approved migration gate.
