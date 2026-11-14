# S-016: Bundled Grafana dashboards and `logging open`

status: built
serves: G-1, G-3

## Problem

A deployed `LoggingStack` collects every server's journal and host metrics, and Grafana starts
with two provisioned data sources (`Cloudfall Loki`, `Cloudfall Prometheus`) and nothing else. A
person who opens it sees an empty home page: every chart and log view is built by hand in
Explore, from label names they have to know (`server`, `environment`, `unit`, `source`, `job`,
`application`, `component`, `service`). A team that moves from a PaaS expects the dashboard to be
there on the first day.

Reaching Grafana is manual too. It listens on `127.0.0.1` only, by design, so a person writes the
SSH tunnel by hand from the server's address and SSH port, which the project already declares.

## Behaviour

### Bundled dashboards

`logging deploy` provisions a fixed set of dashboards on the backend, through Grafana's file
provider, into a folder named `Cloudfall`:

| uid | Title | Shows |
|---|---|---|
| `cloudfall-fleet` | Fleet | One row per server: up (seconds since its last sample), CPU busy, memory used, fullest filesystem, load, and the firing `ALERTS` series for the environment |
| `cloudfall-server` | Server | One server (variable `server`): CPU by mode, memory, load, each ext4/xfs filesystem's used percent and free bytes, disk I/O, network, and its log volume by `unit` |
| `cloudfall-logs` | Logs | Variables `server`, `unit` and a free-text filter: log volume by unit, lines matching `error\|fail\|panic` by unit, and the log lines themselves |
| `cloudfall-services` | Services | The PostgreSQL and Redis exporters' metrics by `service` (up, connections, transaction and command rates, memory); empty, not broken, when no such service is declared |

- Every dashboard has an `environment` variable and reads its server list from the data
  (`label_values(node_uname_info, server)`), so it works on any project without editing
- Every panel names its data source by the provisioned uid (`cloudfall-loki`,
  `cloudfall-prometheus`), never by name or as the default
- Every query uses only labels the collector writes: `environment`, `server`, `unit`, `source`,
  `job`, `application`, `component`, `service`, plus the exporters' own
- The dashboards ship as JSON files in the backend role; the provider sets
  `allowUiUpdates: false` and `disableDeletion: false`. A person may change a dashboard in the
  browser and "Save as" a copy outside the `Cloudfall` folder; the bundled ones stay as shipped,
  and a dashboard removed from a later release disappears on the next deploy
- The dashboards' `schemaVersion` is one the stack's pinned Grafana reads without migrating it on
  load

### `logging open`

`cloudfall logging open` opens the stack's Grafana for a person:

- It runs `ssh -N -L <local>:<grafana listen address>:<grafana port>` to the backend server, built
  from the server's declared address, SSH user and port, and prints
  `http://127.0.0.1:<local>/dashboards/f/cloudfall` once the forward is up. It stays in the
  foreground until interrupted
- `--port` picks the local port (default `3000`); a port already in use stops it with
  `PRECONDITION` naming the port, before ssh starts
- `--print-command` answers with the ssh command and the URL in the envelope without running
  anything, for an agent or a script
- It never prints the admin password; the answer names where it is kept
  (`secrets/<environment>/logging-stacks/<id>.env`, key `GRAFANA_ADMIN_PASSWORD`)
- With more than one `LoggingStack`, `--stack <id>` is required; with one, it is the default

### Audit

`inspect` records the sha256 of each file in Grafana's Cloudfall dashboards directory on the
backend. `audit` adds `logging.dashboards`: compliant when the set of files and their hashes match
the ones the installed Cloudfall release bundles; drift names a missing, extra or changed file.

## Acceptance criteria

- S-016-1: After `logging deploy`, Grafana's `/api/search?folderUIDs=cloudfall` lists exactly the
  four dashboards with their uids, and a second deploy changes nothing
- S-016-2: On a host whose collector has reported for two collection intervals, every panel of
  `cloudfall-fleet`, `cloudfall-server` and `cloudfall-logs` returns data through Grafana's
  `/api/ds/query` with no error; every panel of `cloudfall-services` returns no error, with data
  only where a PostgreSQL or Redis service is declared
- S-016-3: A test of the bundled JSON fails when a panel names a data source other than the two
  provisioned uids, uses a label outside the list above, or a dashboard lacks the `environment`
  variable
- S-016-4: Saving a change to a bundled dashboard through Grafana's API is refused; a copy saved
  in another folder survives a redeploy
- S-016-5: `logging open --print-command` answers with an ssh command that uses the backend's
  declared address, user and port and forwards to Grafana's declared listen address and port; the
  answer contains no password. `logging open` with the local port in use exits 4 before ssh
  starts
- S-016-6: `audit` reports `logging.dashboards` compliant after S-016-1, and drift when a bundled
  file is removed or edited on the host
- S-016-7: A stack deployed before this release gains the dashboards on its next `logging deploy`
  and nothing else in its Grafana changes

## Out of scope

- Dashboards a project supplies itself (a `dashboards/` folder in the project): a later spec once
  the bundled set has been used
- Exposing Grafana on a domain with TLS and login, users, teams and SSO: Grafana stays on loopback
- Grafana-managed alerting; alerts stay in Prometheus and Alertmanager (S-015)
- Reducing Loki's own query logging, which is most of a quiet host's log volume: a separate issue

## Decisions relied on

- none

## Issues

Built in slices by cloudfall-dev/cloudfall#305, #306 and #307 under the feature issue below,
and proven live on 0.8.0rc34.

- cloudfall-dev/cloudfall#304: S-016-1, S-016-2, S-016-3, S-016-4, S-016-5, S-016-6, S-016-7

## Verification

Built in #305 (dashboards), #306 (`logging open`) and #307 (`logging.dashboards`), released in
0.8.0rc34, and run on a Debian 13 backend on 2026-11-14 (Grafana 13.2.6). Per criterion:

- S-016-1: `test_s016_1_the_role_bundles_exactly_the_four_dashboards`. Live: after
  `logging deploy --yes`, `/api/search?folderUIDs=cloudfall` listed exactly the four uids;
  a second deploy changed no file and restarted no unit
- S-016-2: live: every panel's targets, posted to `/api/ds/query` with the variables filled
  in, answered with no error (28 panels); Fleet, Server and Logs returned data, Services
  returned none on a host with no PostgreSQL or Redis, and "Firing alerts" none with nothing
  firing
- S-016-3: `test_s016_3_*` in `engine/tests/test_s016_dashboards.py`: the `environment`
  variable first, data sources by uid only, and labels limited to the collector's and the
  exporters' own, with a check that catches a foreign label
- S-016-4: `test_s016_4_the_provider_keeps_bundled_dashboards_as_shipped`. Live: saving over
  `cloudfall-fleet` was refused ("Cannot save provisioned dashboard"); a copy saved in
  General survived a redeploy
- S-016-5: `test_s016_5_*` in `sdk/tests/test_s016_logging_open.py`, including the CLI
  holding a fake ssh's forward and stopping it. Live: `--print-command` built the forward
  from the declared address and SSH port, `logging open` came up and `/api/health` answered
  through it, a port in use exited 4, and stopping the command closed ssh
- S-016-6: `test_s016_6_*` in `sdk/tests/test_s016_audit.py`, with the inspect tasks run by
  `ansible-playbook`. Live: `audit` compliant, 34 of 34, `logging.dashboards` compliant; with
  one dashboard file moved aside it reported drift "missing: cloudfall-logs.json", and
  compliant again once it was back
- S-016-7: `test_s016_7_the_role_installs_prunes_and_provisions_the_dashboards`. Live: a stack
  deployed on 0.8.0rc28 gained the dashboards on its next `logging deploy`; the data sources
  and Grafana's configuration were unchanged
