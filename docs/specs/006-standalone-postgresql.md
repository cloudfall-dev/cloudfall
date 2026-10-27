# S-006: Standalone PostgreSQL

status: approved

## Problem

A PostgreSQL `Service` can only hold databases that belong to an application Cloudfall
deploys. `config/schemas/v1/service.schema.json` requires
`spec.postgresql.databases[].application`, `cloudfall config validate` refuses a name with
no `Application` behind it (`sdk/src/cloudfall/validation.py`), and an `Application` needs
at least one component. The database role is the application's Linux user
(`_postgres_database` in `sdk/src/cloudfall/inventory.py`), it has no password, and the
server listens on `127.0.0.1` only (`bind.address` is `const` in the schema and asserted by
`cloudfall_postgresql`). So a team that wants Cloudfall to run only its database, with the
app staying elsewhere, has no way to declare it (cloudfall-dev/cloudfall#81).

## Behaviour

**A database with an owner instead of an application.** Each entry of
`spec.postgresql.databases` names exactly one of `application` (today's form) or `owner`,
a PostgreSQL role Cloudfall creates. `owner` uses the database-name pattern
(`^[a-z][a-z0-9_]*$`, at most 63 characters). An entry with `owner` also names
`passwordRef`, the role's password in the secrets store:
`{environment, path, key}`, read from `<secrets-dir>/<environment><path>.env` as the line
`<key>=<value>`, the same sops and age layout `cloudfall secrets render` reads
(`sdk/src/cloudfall/secrets.py`). An entry with `application` can't carry `passwordRef`.
A project with a server and a PostgreSQL `Service` whose databases all name an `owner`
needs no `Application` or `Component`.

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- an entry with both `application` and `owner`, or with neither
- `owner` without `passwordRef`, or `passwordRef` next to `application`
- an `owner` of `postgres`, of the metrics role (`alloy`), or equal to the Linux user of an
  `Application` that has a database on the same service, with code
  `service_database_owner_reserved`

**The role and its password.** `cloudfall_postgresql` creates the `owner` role with
`LOGIN` like an application role today, then sets its password on every converge, so a
rotation is a commit in the secrets repository and a services converge. The value is
decrypted on the controller by the path that runs `services.yml` (the `converge_services`
MCP tool, the `services` step of `cloudfall migrate`, the operator's `converge-services`
operation, and `cloudfall-engine playbook run services.yml`), each reading the secrets
directory from a `--secrets-dir` flag (default `secrets`, as `secrets render` and
`mcp serve` use). It reaches the host in a `0600` file removed after the run, the role
sends it to `psql` on stdin with `no_log`, and PostgreSQL stores it as SCRAM. The value
never appears in the rendered inventory, a command line, Ansible output, a command's
answer, or a receipt; the answer names the reference, the key, and the value's SHA-256,
as `EnvironmentReceipt` does for environment files. A missing file or key stops the run
with `PRECONDITION` naming the expected path and key before any playbook starts.

**A way in for an outside app.** `spec.postgresql.remoteAccess` is optional:

```yaml
remoteAccess:
  listenAddress: 10.0.0.2
  allowedSources:
    - 10.0.0.0/24
```

`listenAddress` is one IPv4 or IPv6 address on the server, never a wildcard.
`allowedSources` is a non-empty list of CIDRs. `bind` keeps its loopback contract, and the
same `bind.port` serves both addresses. With `remoteAccess` declared:

- `cloudfall.conf` sets `listen_addresses = '127.0.0.1,<listenAddress>'` and `ssl = on`
  with the Debian `ssl-cert` snakeoil pair, which the role installs
- `pg_hba.conf` gains a block between Cloudfall markers with one
  `hostssl <database> <owner> <cidr> scram-sha-256` line per `owner` database and source.
  `hostssl` means a remote client must use TLS; a plain connection matches no line and is
  refused. Databases named by `application` get no line and stay socket-only with peer
  authentication
- the server's nftables ruleset gains one rule per source, `ip saddr <cidr> tcp dport
  <port> accept` (`ip6 saddr` for an IPv6 CIDR), rendered by
  `engine/src/cloudfall_engine/ansible_inventory.py` into `cloudfall_firewall.allowedInbound`
  as entries with an optional `sources` list, and applied when `baseline.yml` runs. Entries
  without `sources` render as today

`cloudfall config validate` refuses `remoteAccess` with `CONFIG_INVALID` when no database
on the service names an `owner` (`service_remote_access_unused`), when the server's server
type declares no `firewall` (`service_remote_access_unfirewalled`), when that firewall's
`allowedInbound` already opens the service port to everyone
(`service_remote_access_port_open`), or when a source is `0.0.0.0/0` or `::/0`
(`service_remote_access_open`). With `remoteAccess` absent, PostgreSQL listens on
`127.0.0.1` only, `pg_hba.conf` keeps no Cloudfall block (a block left by an earlier
declaration is removed), and no PostgreSQL rule is added to nftables. Closed is the default.

**Backups, the restore drill, and `data copy`.** They don't change. `backup.sh.j2` and
`restore-check.sh.j2` loop over `postgresql.databases` by `name` only, and
`cloudfall_data_migration` selects the entry by name and restores with
`--role <owner>`; its receipt records `service`, `database`, and `owner`, never an
application. An `owner` database is backed up, drilled, and copied into the same way.

**Compatibility.** Every config that validates today validates the same. A database entry
with `application` renders exactly as before; one with `owner` renders `name` and `owner`
with no `application` key. The role's argument spec
(`engine/ansible/roles/cloudfall_postgresql/meta/argument_specs.yml`) makes `application`
optional and adds `remoteAccess`. The rendered service carries `passwordRef`, never the value;
the password travels as its own role variable.

Touches `config/schemas/v1/service.schema.json`, `sdk/src/cloudfall/validation.py`,
`sdk/src/cloudfall/inventory.py` (`PostgresDatabase`, `_postgres_database`,
`ServiceInventory`), `sdk/src/cloudfall/secrets.py`, `sdk/src/cloudfall/migrate.py`,
`sdk/src/cloudfall/app.py` (`converge_services`, `cloudfall migrate`),
`sdk/src/cloudfall/operator.py`, `engine/src/cloudfall_engine/ansible_inventory.py`,
`engine/src/cloudfall_engine/cli.py` (`playbook run`),
`engine/ansible/roles/cloudfall_postgresql`, `engine/ansible/roles/cloudfall_firewall`,
`engine/ansible/playbooks/services.yml`, and `docs/secrets-guide.md`.

## Acceptance criteria

- S-006-1: A project with one `Server`, its server type, and one PostgreSQL `Service` whose only database declares `owner: billing` and a `passwordRef`, and no `Application` or `Component`, passes `cloudfall config validate` with exit 0
- S-006-2: A database entry with both `application` and `owner` fails `cloudfall config validate` with `CONFIG_INVALID` naming `spec.postgresql.databases[0]`, and an entry with neither fails the same way
- S-006-3: `owner` without `passwordRef` fails `cloudfall config validate` with `CONFIG_INVALID`, and so does `passwordRef` next to `application`
- S-006-4: An `owner` of `postgres`, of `alloy`, or equal to the Linux user of an `Application` with a database on the same service fails with `CONFIG_INVALID` code `service_database_owner_reserved`
- S-006-5: After `services.yml` converges the service of S-006-1, `pg_roles` holds `billing` with `rolcanlogin` and a `SCRAM-SHA-256` password, and database `billing` is owned by `billing`
- S-006-6: The password value appears in none of the rendered inventory, the playbook output, the command's answer, or any receipt, and the answer names the reference, the key, and the value's SHA-256
- S-006-7: A `passwordRef` whose file or key is missing stops the services converge with `PRECONDITION` naming the expected path and key, and no playbook runs
- S-006-8: With `remoteAccess` of `listenAddress: 10.0.0.2` and `allowedSources: [10.0.0.0/24]`, the converged `cloudfall.conf` reads `listen_addresses = '127.0.0.1,10.0.0.2'` and `ssl = on`, the Cloudfall block in `pg_hba.conf` holds exactly `hostssl billing billing 10.0.0.0/24 scram-sha-256`, and the rendered nftables ruleset holds `ip saddr 10.0.0.0/24 tcp dport 5432 accept` and no unrestricted rule for port 5432
- S-006-9: On a proving pair of hosts, `psql` from an address in `allowedSources` as `billing` with `sslmode=require` connects, from an address outside it gets no connection, and from an allowed address with `sslmode=disable` is refused with `no pg_hba.conf entry`
- S-006-10: Removing `remoteAccess` and converging baseline and services leaves `listen_addresses = '127.0.0.1'`, no Cloudfall block in `pg_hba.conf`, and no rule for the service port in nftables
- S-006-11: `remoteAccess` fails `cloudfall config validate` with `CONFIG_INVALID` and code `service_remote_access_unused` when no database names an `owner`, `service_remote_access_unfirewalled` when the server type declares no `firewall`, `service_remote_access_port_open` when its `allowedInbound` opens the service port, and `service_remote_access_open` for a source of `0.0.0.0/0` or `::/0`
- S-006-12: For the project in `config/examples`, `cloudfall-engine inventory render` writes a byte-identical inventory before and after the change, and the rendered `cloudfall.conf`, `nftables.conf`, backup script, and restore-check script are identical
- S-006-13: For the service of S-006-1, the backup script writes `billing-<timestamp>.dump`, the restore drill restores it and passes, and `cloudfall data copy <service> --database billing --source-url-file <file> --yes` restores as role `billing` and writes a receipt with `spec.owner` of `billing`

## Out of scope

- Managed connection pooling (PgBouncer): a separate service with its own auth file, and nothing in the issue needs it yet
- Replication and read replicas: a different durability contract from the dump-based backup and drill
- A CA-verified server certificate: the snakeoil pair gives encryption with `sslmode=require`, not server identity with `verify-full`. A later spec can take a certificate from the domain's ACME issuance
- A tunnel (WireGuard or SSH) as the way in: the listen address plus source list covers a private network and a fixed egress address, which is what the proving run will use
- Remote access for databases named by `application`: their role has no password and peer authentication is the point of that form
- Restoring the "for an app elsewhere" path in `docs/neon-migration-guide.md`: it follows a proving run of S-006-9, not this spec

## Decisions relied on

- none

## Issues

- cloudfall-dev/cloudfall#121: S-006-1, S-006-2, S-006-3, S-006-4, S-006-11
- cloudfall-dev/cloudfall#122: S-006-5, S-006-6, S-006-7, S-006-12, S-006-13
- cloudfall-dev/cloudfall#123: S-006-8, S-006-9, S-006-10

## Verification
