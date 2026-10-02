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

Active environment-scoped SSH public keys are rendered as
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
`--tags`, and `--extra-vars` pass through to `ansible-playbook`. At a terminal,
Ansible's play log streams to stderr as it runs; elsewhere, such as under an
agent or in CI, pass `-v` to see it. stdout carries one JSON document when the
run ends, and a failed run keeps the end of the log in `error.context.output`.
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
firewall are left unmanaged.

The access role installs active environment-scoped `SshPublicKey` resources
for root, validates the complete OpenSSH configuration, and only then
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
calendar with bounded retention, and
`/usr/local/sbin/cloudfall-postgresql-restore-check` proves the newest dump
of every database restores into a scratch database before dropping it:

```console
sudo -u postgres /usr/local/sbin/cloudfall-postgresql-restore-check
```

## Build and deploy component releases

`cloudfall-engine artifact build` clones a component's declared repository at
an explicit git ref, packages the source as a hashed tarball (without `.git`),
and writes schema-valid artifact metadata beside it:

```console
task artifact:build COMPONENT=crm-backend REF=main
```

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
`health.yml` playbook contracts. Rollback activates an already-retained
release without re-transferring an artifact and requires the declared health
check to pass; it deliberately does not auto-restore on failure, because a
failed rollback is an incident, not a deployment.

The deploy role transfers the artifact and refuses it if the digest does not
match the build metadata, unpacks it under `releases/<release-id>/`,
materializes locked dependencies with a pinned `uv` (`uv sync --frozen
--no-dev`, with the managed Python toolchain and cache under the component's
`shared/` directory), optionally installs an environment file, renders the
systemd unit from the declared service command, and then activates behind a
health gate: symlink switch, restart, and the declared HTTP health check. If
the health check fails, the previous release is restored and restarted
automatically and the run fails; a first release with no predecessor is
stopped instead. Successful runs write a schema-valid release receipt on the
controller. The first slice supports Python components managed by uv on
x86_64 Debian hosts.

## Public domain routes

`ansible/playbooks/domains.yml` renders every declared `Domain` as a managed
Nginx virtual host on its proxy server:

```console
task domains:deploy:check
task domains:deploy
task domains:deploy ISSUE_CERTIFICATES=true
```

The site role installs Nginx and Certbot, serves the ACME challenge webroot,
and converges in two phases: until a certificate exists the route serves
plain HTTP so the site stays reachable during cutover; once TLS material is
present (or issuance is explicitly requested via `ISSUE_CERTIFICATES=true`)
HTTP redirects to HTTPS with TLS 1.2+. The proxy discards any
client-supplied forwarding chain and sends only the canonical
`$remote_addr` as `X-Forwarded-For` and `X-Real-IP`, so upstream
applications cannot be spoofed by forged headers. When TLS is required the
`certbot.timer` renewal unit is enabled, and every successful run writes a
schema-valid deployment receipt on the controller; a failed run never emits
a receipt.

## Read-only inspection role

`ansible/roles/cloudfall_inspect` collects normalized evidence without changing the
remote host. It reads Ansible facts, the Debian package database through
`dpkg-query`, service facts, systemd timer units and unit files, the managed
`inet cloudfall` nftables table as JSON, listening TCP/UDP sockets from
`ss -tulnH`, `lsblk`, `findmnt`, `/proc/mdstat`,
`mdadm --detail --scan`, read-only NVMe reports from `smartctl`, and `stat` data for the
configuration paths allowlisted by the server's `ServerType`.

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
