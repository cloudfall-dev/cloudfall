# Engine

This module executes validated Cloudfall operations using Ansible. It owns
inventories, playbooks, roles, templates, deployment health checks, symlink
switching, and rollback mechanics.

The engine acts on explicit inputs and must not silently modify declarative
state.

## Parallel observability pilot

`ansible/playbooks/logging.yml` installs pinned Loki and Grafana packages plus
Prometheus on one declared backend, exposes only the Loki push and Prometheus
remote-write endpoints through an mTLS Nginx gateway, and installs Alloy on
declared collectors. Alloy ships journal and file logs and pushes host
metrics from its embedded unix exporter, so collectors open no metrics port.
Loki, Prometheus, and Grafana bind to loopback, and Grafana is provisioned
with both data sources. The roles validate pre-provisioned secret files and
native service configuration before restart, deploy one host at a time, and
never manage Filebeat or the existing Elastic path:

```console
task logging:check CONFIG_DIR=config/examples
task logging:deploy CONFIG_DIR=config/examples
```

Follow the [`logging service guide`](../docs/logging-service-guide.md) before
declaring a `LoggingStack` for live hosts.

Its first executable capability is deterministic Ansible inventory rendering:

```console
uv run cloudfall-engine inventory render --project config/examples
```

The renderer consumes the SDK's typed inventory and creates host variables plus
environment, application, and component groups. It never includes secret values or
secret references.

Environment-scoped SSH public keys, active and revoked, are rendered as
`cloudfall_ssh_public_keys`. Rendering alone does not change authorized keys.

## Running playbooks

The engine's `ansible.cfg`, playbooks, and roles ship inside the installed
package. `cloudfall-engine playbook run` executes a bundled playbook by name
or any playbook file by path under that configuration, and always searches
the bundled roles last:

```console
uv run cloudfall-engine playbook list
uv run cloudfall-engine playbook run inspect --inventory tmp/ansible-inventory.json \
  --extra-vars cloudfall_inspect_output_directory=$PWD/tmp/observed
uv run cloudfall-engine playbook run engine/ansible/playbooks/proxy.yml \
  --roles engine/ansible/roles --limit proxy --tags nginx
```

`--dry-run` (Ansible's check mode), `--diff`, `--syntax-check`, `--limit`,
`--tags`, and `--extra-vars` pass through to `ansible-playbook`. Ansible's play
log streams to stderr as it runs, as plain text whether or not a terminal is
attached (`--quiet` silences it). stdout carries one JSON document when the run
ends, and a failed run keeps the end of the log in `error.context.output`,
marked as untrusted content from the hosts.
`cloudfall-engine doctor` checks that ansible-playbook and git are installed.
A project uses this instead of
setting `ANSIBLE_CONFIG` against a checkout; the `task` targets in this
repository still call `ansible-playbook` directly because they run from the
source tree.

## Output and exit codes

`cloudfall-engine` is built on [treaty](https://github.com/romamo/treaty): every
command answers one JSON envelope on stdout (`ok`, `data`, `error`, `warnings`,
`meta`) and `cloudfall-engine manifest` describes every command, flag, and exit
code. Input errors exit 2 before anything runs. Engine failures have their own
codes, with the original snake_case code in `error.context.code`:

| Exit | Code | Meaning |
|---|---|---|
| 79 | `PROJECT_INVALID` | no project directory could be resolved |
| 80 | `CONFIG_INVALID` | the project's resources failed validation |
| 81 | `ARTIFACT_BUILD_FAILED` | the component could not be cloned or packaged |
| 82 | `PLAYBOOK_FAILED` | `ansible-playbook` exited non-zero; `error.context.returncode` has its code |

Relative paths such as `--output tmp/ansible-inventory.json` and the default
`--output-dir tmp/artifacts` land in the project, whatever the working directory.

## UTC time baseline

`ansible/playbooks/time.yml` enforces `Etc/UTC`, keeps the hardware clock in
UTC, enables systemd network time synchronization, and waits for the host clock
to synchronize. The play targets the complete Cloudfall inventory and runs one host
at a time; use `TIME_LIMIT` for a bounded deployment:

```console
task time:check CONFIG_DIR=config/examples TIME_LIMIT=h1
task time:deploy CONFIG_DIR=config/examples TIME_LIMIT=h1
```

## Server baseline

`ansible/playbooks/baseline.yml` converges a host to the managed baseline in
one run: bootstrap, the UTC time contract, declared key-only SSH access,
unattended Debian security upgrades, and the declared default-deny firewall.
It targets all servers serially and is idempotent; a second run reports no
changes:

```console
task server:baseline:check
task server:baseline
```

The firewall role renders `/etc/nftables.conf` from the rendered
`cloudfall_firewall` host variable (declared profile rules plus the server's
SSH port), validates it with `nft --check` before installation, and refuses
any ruleset that does not allow the control connection's SSH port. The
managed table replaces the complete ruleset; hosts whose profile declares no
firewall are left unmanaged. An entry with a `sources` list (a PostgreSQL
service's `remoteAccess`) renders one `ip saddr`/`ip6 saddr` rule per source
and never counts as the SSH port.

The access role installs active environment-scoped `SshPublicKey` resources
for root, then removes the revoked ones (undeclared keys stay), validates the
complete OpenSSH configuration, and only then
disables password and keyboard-interactive authentication.

## Bootstrap role

`ansible/roles/cloudfall_bootstrap` prepares an already-installed Debian host for
application deployment. It:

- validates that the host is Debian with systemd;
- installs a minimal baseline package set without upgrading the OS;
- creates one system account per application;
- creates application shared directories; and
- creates component `releases/` and `shared/` directories.

It deliberately does not install Debian, configure RAID, change SSH or firewall
policy, fetch secrets, deploy artifacts, or start application services.

## Infrastructure services

`ansible/playbooks/services.yml` converges every declared `Service` resource
on its server. The first catalog entry is PostgreSQL:

```console
task services:deploy:check
task services:deploy
```

The PostgreSQL role installs the declared major version from the PGDG
repository (optionally pinned), binds the cluster to loopback through a
managed `conf.d` drop-in, and creates one login role and database per
declared application using peer authentication over the local socket — no
database passwords exist anywhere. Components connect as their application's
Linux user. Backups run `pg_dump` in custom format on the declared systemd
calendar with bounded retention, writing `<database>-<timestamp>.dump` per
database and, beside them in the same backup directory,
`globals-<timestamp>.sql` from `pg_dumpall --globals-only`: the roles and
other cluster-wide objects no per-database dump holds, without role
passwords (converge sets each owner role's password from its secret).
`/usr/local/sbin/cloudfall-postgresql-restore-check` proves the newest dump
of every database restores into a scratch database
(`<database>_restore_check`, the name cut to its first 49 characters) before
dropping it: every table the dump holds must come back with the row count
the dump holds, and a dump without a table fails the drill. No declared
database may carry another's scratch name:

```console
sudo -u postgres /usr/local/sbin/cloudfall-postgresql-restore-check
```

To restore onto a fresh server, load the globals first, then each database:

```console
sudo -u postgres psql --file globals-<timestamp>.sql postgres
sudo -u postgres pg_restore --create --dbname postgres <database>-<timestamp>.dump
```

A service that declares `postgresql.remoteAccess` also listens on its
`listenAddress` with `ssl = on` (the `ssl-cert` snakeoil pair); a change to
the listener restarts the cluster. Its `pg_hba.conf` gains a block between
Cloudfall markers with one `hostssl` SCRAM line per owned database and source,
applied with `pg_reload_conf()` only after `pg_hba_file_rules` reports no
error; a service without `remoteAccess` has the block removed.

## Build and deploy component releases

`cloudfall-engine artifact build` clones a component's declared repository at
an explicit git ref, packages the source as a hashed tarball (without `.git`),
and writes schema-valid artifact metadata beside it:

```console
task artifact:build COMPONENT=crm-backend REF=main
```

A component that declares `spec.build` (with `spec.buildTools` and
`spec.artifact.include`, see `docs/specs/002-component-build-steps.md`) is
built on the controller before it is packaged. Every `buildTools` entry must
be on `PATH` at its minimum version before anything is cloned; then each step
runs in order in the clean checkout, with only `PATH`, `HOME`, `LANG` and
`TMPDIR` from the controller's environment plus `CI=true`, so cloud tokens and
SOPS keys stay out of `package.json` scripts. Step output goes to stderr. The
archive holds the files git tracks plus the `artifact.include` paths, such as
a gitignored `web/dist`, and the metadata records the steps and the tool
versions under `spec.build`. A failed or timed-out step, a missing include
path, an include path that leads out of the checkout through a symlink, or a
tracked file a step deleted writes no archive. The command may take
1800 seconds for the clone plus every step's `timeoutSeconds`; an explicit
`--timeout` replaces that limit, and `--timeout 0` disables it.

`ansible/playbooks/deploy.yml` deploys one built release to the component's
declared servers, one host at a time:

```console
task deploy COMPONENT=crm-backend RELEASE=<release-id>
task deploy COMPONENT=crm-backend RELEASE=<release-id> ENV_FILE=/path/to/env
task rollback COMPONENT=crm-backend RELEASE=<previous-release-id>
task restart COMPONENT=crm-backend
task health COMPONENT=crm-backend
```

These tasks call the SDK's lifecycle commands, which verify the artifact and
then execute the engine's `deploy.yml`, `rollback.yml`, `restart.yml`, and
`health.yml` playbook contracts.

A component's `release.command` (database migrations, say) runs once per
deploy, on the first server of `deployment.servers`, which deploys before the
others. It runs in a transient systemd unit as the application's user, in the
new release directory (or its `workingDirectory`), with the new release's
environment file (below) loaded by the unit's own `EnvironmentFile=`
rules, and is stopped after `timeoutSeconds`. A failure or a timeout fails the
deploy before any server's `current` moves; a success is recorded under
`releaseCommand` in the release receipt of that deploy. The command must be
safe while the old release still serves traffic, and it must work with the
previous release too: nothing reverts it, so when a server's health gate fails
later in the deploy, every server goes back to its previous release against
what the command changed, and the failure message says the command already
ran. `deploy` without `--yes` shows
it as `release_command` (command, resolved working directory, timeout and
server), `null` for a component without one. Rollback never runs the release
command, and its plan says so with `release_command_skipped: true`. Rollback activates an already-retained
release without re-transferring an artifact and requires the declared health
check to pass; it deliberately does not auto-restore on failure, because a
failed rollback is an incident, not a deployment. Before the restart it points
the unit's `EnvironmentFile=` line at the target release's own environment
file, or at the legacy one when its deploy wrote none, so the release comes
back with the environment it was deployed with. A rollback to a release that
is no longer on the server (never deployed there, or removed by a deploy's
cleanup) is refused with a message naming the releases still kept there.

The deploy role transfers the artifact and refuses it if the digest does not
match the build metadata, unpacks it under `releases/<release-id>/` (or,
when `current` already runs that release, under a fresh
`releases/<release-id>.redeploy-<UTC time to the microsecond>/`, so the live
directory is never written into; a redeploy copy that fails its health gate
is removed once the previous copy runs again, so a rollback to that release
id takes its newest copy that passed),
materializes locked dependencies with a pinned `uv` (`uv sync --frozen
--no-dev`, with the managed Python toolchain and cache under the component's
`shared/` directory), optionally installs an environment file, runs the
component's `release.command` when it declares one, renders the
systemd unit from the declared service command, and then activates behind a
health gate: symlink switch, restart, and the declared HTTP health check. The
environment file is the release's own,
`shared/env/<component>.<release directory>.env`
(`root:<application user>`, `0640`), which the release's unit,
its release command and its command health check read; a redeploy copy gets
one of its own. A deploy without an environment file names the legacy
`shared/env/<component>.env` in the unit, as before, and no deploy writes or
removes that file, so a release deployed before per-release files still finds
it. If the health check fails, the previous release is restored with the
service unit it ran under, and with it the environment file that unit names,
restarted, and gated on its own health check, and the run fails;
a restored release that fails that check fails the run with its own message. A
first release with no predecessor is stopped instead. A component on several
servers deploys one server at a time; when a later server fails its gate, the
servers that switched to the release earlier in the run go back the same way,
last one first, each with the unit it ran under and gated again, and the
failure names them (and any whose restored release failed its gate again). A
server the run never switched is left alone. A unit of the same name that Cloudfall did not render (no
`# Managed by Cloudfall` header, such as a hand-made unit adopted on a
brownfield move) is kept at `/etc/systemd/system/<name>.service.pre-cloudfall`
before the managed unit replaces it, together with whether it was enabled and
active; when that first release fails its health check, the kept unit is
copied back, its enabled state restored, and it is started again if it was
running. The kept copy stays after a successful deploy and is never
overwritten; a later hand-made unit that differs from it refuses the deploy.
Check mode only reports the unit it would keep, and the uv release it would
install; it transfers and unpacks nothing, so it skips the digest check and
`uv sync`. A deploy writes one schema-valid release receipt per component
on the controller, `<receipts>/<component>.json`, with the deployed `release`
and one entry per server under `servers`, in the order they deployed: the
`release` that server ended on (`null` for a stopped first release), its
`previousRelease`, whether its `healthCheckPassed`, and whether it was
`rolledBack`. The controller keeps the run's entries and writes the whole file
again after each server, so the receipt always names every server of the run;
a receipt written before entries, with one `server`, still validates and
reads as a one-entry list.

Once a server passes its health gate, the deploy keeps the component's
`deployment.retainReleases` releases under its `releases/` (an integer, at
least 2, 5 when absent; `retainUntilCleanup: true` means this cleanup is on)
and removes the older ones. A release is a release id: `releases/<id>` and its
redeploy copies stay or go together. The deployed release, the one `current`
points to and the one before it (what `current` pointed to before this deploy,
which a rollback of that server takes) always stay, whatever the count and
whatever their order, since after a rollback `current` need not be the newest;
the other releases stay newest first by release id until the count is
reached. A directory under `releases/` whose name is not a release id is
never touched. A removed release's environment files under `shared/env/` go
with it; the legacy `shared/env/<component>.env` stays. The removed ids are
that server's `removedReleases` in the
receipt (an empty list when nothing was removed). A failed gate removes
nothing, and check mode only reports what a deploy would remove.

A component's optional `service.stop.timeoutSeconds` (1 to 1800) is its
unit's `TimeoutStopSec=`, how long systemd lets the service stop on SIGTERM
before it sends SIGKILL; without it the unit sets none and keeps systemd's
default. A deploy writes it into the unit; `restart` and `rollback` restart
the unit as it was deployed. The runner's limit on the playbook run grows by
the timeout for each restart the run may make, one per server for `restart`
and `rollback`, two per server for a deploy, whose failed gate restarts the
servers back.

A `healthCheck` of `type: command` (a smoke check, say) is the gate's last
step: after the switch and the restart, the command runs in a transient
systemd unit, `cloudfall-health-<component>`, as the application's user, in
`<installRoot>/current` (a relative first element of `command` resolves
there), with the environment file of the release `current` points to (the
one its unit's `EnvironmentFile=-` line names), and is stopped
after `timeoutSeconds`. It runs up to `attempts` times, 3 seconds apart, and
passes at the first exit 0. Its stdout and stderr stay in the server's
journal (`journalctl -u cloudfall-health-<component>`) and never reach the
receipt or the Ansible output. When every attempt fails, the rescue above
restores the previous release (or stops a first one), writes the server's
receipt entry with `healthCheckPassed: false`, the last exit code or the
timeout, and `release` naming the release that serves again, and fails the
run. The entry of a passing command check records it under `healthCheck` too;
`http` and `none` checks keep their receipts and their gate as before.
`restart`, `rollback` and `health` run the same command check through
`health_gate.yml`, with the same unit, limits and redaction, and write no
receipt: `health` reports `unhealthy` when it exits non-zero, and a failed
check fails `restart` or `rollback` without restoring anything, as an `http`
check does. The check runs as root to start its unit, so `health` of a
component with a command check needs the inventory SSH account to become
root, as `restart` and `rollback` already do. The check must be safe to run
at any time against the live service.

The first slice supports Python components managed by uv on
x86_64 Debian hosts.

## Public domain routes

`ansible/playbooks/domains.yml` renders every declared `Domain` as a managed
Nginx virtual host on its proxy server:

```console
task domains:deploy:check
task domains:deploy
task domains:deploy ISSUE_CERTIFICATES=true
```

`proxy.upstream` and `routes` are optional in the schema, for a Domain the
fleet routes with its own proxy, but the site role needs one of them. Before it
changes any server, the playbook fails and names every Domain of the run that
has neither.

A Domain with `routes` renders one `location` per route, longest path first.
A proxy route passes the full request path to its `upstream`; a static route
serves `<installRoot>/current/<directory>` of its component, so a deploy or a
rollback switches what it serves with no Nginx reload, and answers its
`spaFallback` for an unknown path. Without a `/` route, a path no route
matches answers 404. The role installs `acl` and grants the
Nginx worker (`www-data`) traverse-only access (`user:www-data:--x`) from the
application root to the install root and its `releases/`; the worker joins no
group, so the environment files under `shared/env/` stay unreadable to it.

A Domain's `proxy.maxRequestBodyMegabytes` (an integer, 1 to 10240) sets the
largest request body its vhost accepts: every `server` block of the Domain
renders `client_max_body_size <n>m;`, covering all its routes. Without it,
Nginx keeps its 1 MB default and answers 413 to a larger upload. A Domain with
`edge.mode: proxied` is still capped by the edge provider's own upload limit
(Cloudflare's is 100 MB on its Free and Pro plans).

The site role installs Nginx and Certbot, serves the ACME challenge webroot,
and converges in two phases: until a certificate exists the route serves
plain HTTP so the site stays reachable during cutover; once TLS material is
present (or issuance is explicitly requested via `ISSUE_CERTIFICATES=true`)
HTTP redirects to HTTPS with TLS 1.2+. The HTTPS server serves HTTP/2 by
default, so install the fixed Nginx from Debian's security suite first
(CVE-2026-123007: bookworm `1.22.1-9+deb12u11`, trixie
`1.26.3-3+deb13u11`). To serve HTTP/1.1 only, set
`cloudfall_nginx_site_http2: false` in the inventory's host or group vars,
or pass `--extra-vars cloudfall_nginx_site_http2=false` to
`ansible-playbook`. The proxy discards any
client-supplied forwarding chain and sends only the canonical
`$remote_addr` as `X-Forwarded-For` and `X-Real-IP`, so upstream
applications cannot be spoofed by forged headers. When TLS is required the
`certbot.timer` renewal unit is enabled, and every successful run writes a
schema-valid deployment receipt on the controller; a failed run never emits
a receipt.

`ansible/playbooks/domains-observe.yml` reads one Domain
(`cloudfall_domains_domain_id`) on its proxy server without changing it: the
vhost file's sha256, the certificate's expiry (`openssl x509 -enddate`) and
whether `certbot.timer` is enabled and active. Every server task is a `stat`
or a command with `changed_when: false` that also runs in check mode; the one
write is the reading, handed back to the controller at
`cloudfall_domains_observe_reading`. `cloudfall domains observe` runs it.

## Read-only inspection role

`ansible/roles/cloudfall_inspect` collects normalized evidence without changing the
remote host. It reads Ansible facts, the Debian package database through
`dpkg-query`, service facts, systemd timer units and unit files, the managed
`inet cloudfall` nftables table as JSON, listening TCP/UDP sockets from
`ss -tulnH`, `lsblk`, `findmnt`, `/proc/mdstat`,
`mdadm --detail --scan`, read-only NVMe reports from `smartctl`, and `stat` data for the
configuration paths allowlisted by the server's `Baseline`.

Configuration capture is deliberately bounded to metadata and optional SHA-256
hashes; the role does not fetch file contents. The only persistent writes are
controller-side JSON snapshots, stored with mode `0600`:

```console
task inspect OBSERVATION_DIR=tmp/observed
```

`OBSERVATION_DIR` is repository-relative; the Task workflow passes an absolute
controller path to Ansible so delegated writes cannot resolve relative to the
playbook directory.

The play requires the inventory SSH account to connect and become root. A
collection failure stops the run instead of producing partial evidence that
could be mistaken for a complete audit.

Generated inventory keeps application and component collections in host variables.
This prevents variable collisions when one server belongs to multiple application
or component groups.
