# Run a MariaDB service

Status: standalone servers, primary and replica pairs, promotion, backups and
the restore drill are built and converge on real hosts. A replica of a server
Cloudfall does not manage is declared and validated, but its converge is not
built yet (section 8).

A MariaDB `Service` is one server: standalone, a `primary`, or a `replica` of a
primary in the same project. There is no cluster and no automatic failover. A
role changes only when you change the declaration and converge the services,
so every promotion is one an operator made, and the converge refuses any role
change that would lose or overwrite data. The behaviour is specified in
[spec 019](specs/019-mariadb-service.md).

## Safety boundary

- Only long-term series MariaDB still maintains install: `10.11`, `11.4`,
  `11.8` and `12.3`. MariaDB publishes `10.11` and `11.4` for Debian 12 only;
  Debian 13 gets `11.8` and `12.3`
- `version` is an exact `X.Y.Z` from MariaDB's archive, held with
  `apt-mark hold`, so an unattended upgrade never restarts the server. A
  patch inside the series is a declaration change; a change of series is
  refused (seed a new server as a replica of the old one and promote it)
- The data directory `/var/lib/mysql` lives on the mirrored `mariadb` volume
  of the baseline's storage pool
- Cloudfall's own account is `root` through the unix socket; no password
  exists for it
- A database or user Cloudfall created and you no longer declare is never
  dropped: its user is locked and `audit` lists both as undeclared. Dropping
  data is never a side effect of a converge
- A server that holds a database is never seeded and never emptied

## 1. Prepare the server

The server's `Baseline` declares one storage pool (`storage.pools`) with a
`secondary` physical volume, since the volume is mirrored. Validation refuses a
baseline with no pool (`volume_requirement_pool_missing`), several
(`volume_requirement_pool_ambiguous`), or one without a secondary
(`volume_requirement_pool_unmirrored`).

A server of a replicating pair also needs a declared `firewall` in its
baseline (`service_mariadb_replication_unfirewalled`), and a replica's server
needs an IPv4 its primary can admit: a `network` block or an IPv4 `address`
(`service_mariadb_replica_unaddressed`). One server holds at most one MariaDB
service (`service_mariadb_server_shared`).

Converge the baseline once, as for any server:

```console
uv run cloudfall baseline converge --yes
```

## 2. Declare a standalone server

```yaml
apiVersion: cloudfall/v1
kind: Service
metadata:
  id: mariadb-main
  description: CRM and billing databases
spec:
  serviceKind: mariadb
  environment: production
  server: h1
  bind:
    address: 127.0.0.1
    port: 3306
  mariadb:
    version: 11.8.9
    innodbBufferPoolMb: 4096
    databases:
      - name: crm
        application: crm
      - name: billing
        owner: billing
        passwordRef: { environment: production, path: /services/mariadb-main, key: BILLING_PASSWORD }
  backup:
    directory: /var/backups/cloudfall/mariadb-main
    onCalendar: "*-*-* 02:30:00 UTC"
    restoreCheckOnCalendar: "*-*-* 05:15:00 UTC"
    retentionDays: 14
  metrics:
    enabled: true
```

| Field | Meaning |
| --- | --- |
| `bind.address` | `127.0.0.1`; the role refuses any other. A listener off the host comes from `remoteAccess` or `replication.listenAddress` |
| `version` | An exact release of a supported series (`service_mariadb_version` otherwise) |
| `innodbBufferPoolMb` | `innodb_buffer_pool_size`, 128 or more. The converge refuses, before it changes the host, a value above three quarters of the server's memory |
| `databases` | Each entry names an `application` or an `owner` with a `passwordRef`, never both |
| `backup` | Required, as for every service (section 5) |
| `metrics.enabled` | The logging collector on the server scrapes MariaDB as the socket user `alloy`, which may read status and replication state only |

A database with `application` belongs to a user named after the application's
Linux user, who connects through the socket with no password and never over
TCP. A database with `owner` belongs to that user, whose password comes from
`passwordRef`. Store the password at that reference as the [secrets
guide](secrets-guide.md) describes. The controller computes the password's hash
and only the hash reaches the host, so the password is never staged on a host
or sent in a statement. The password is set on every converge, so a rotation
is a commit in the secrets repository and a services converge.

The hash is `mysql_native_password`, which every client library speaks as
shipped. Add `authentication: ed25519` to an `owner` entry for the stronger
form, when the application's client supports MariaDB's `client_ed25519`
plugin, as MariaDB's own connectors do. Check your driver's documentation
before you switch.

Validation also refuses an `owner` or database named `root`, `mysql`,
`mariadb.sys`, `cloudfall_repl`, `alloy`, a system database, or the Linux user
of an application with a database on the same service
(`service_database_owner_reserved`). `config validate` warns
`MARIADB_VERSION_ENDING` within 90 days of a series' end of life, and `audit`
reports the service once that date has passed.

### Remote access

`remoteAccess` takes the `listenAddress` and `allowedSources` PostgreSQL uses.
Each `owner` gets one account per allowed source that connects only over TLS,
with the server's certificate from the project CA; `application` users stay
socket-only, and the firewall admits the port from the allowed sources only.
An IPv6 network in `allowedSources`, such as `2001:db8::/64`, is refused
(`service_mariadb_remote_access_ipv6`, D-26): MariaDB has no netmask form for
an IPv6 account host. A single IPv6 address (`/128`) and IPv4 networks are
accepted.

## 3. Validate and converge

```console
uv run cloudfall config validate
uv run cloudfall requirements show
uv run cloudfall secrets generate
```

`requirements show` lists what the service needs from its server: the
`mariadb` volume, the `mariadb-server` package at `version`, the port of a
primary open to its replicas, and the certificate of a server with
`remoteAccess` or `replication`. `secrets generate` stores the replication
password of each primary (section 4); it never overwrites a stored key.

A services converge runs `services.yml` through Cloudfall: the
`converge_services` tool of `cloudfall mcp serve`, which an agent calls, or
`cloudfall operator approve` of a services proposal, which converges that
proposal's server alone. Either reads every password from `--secrets-dir`
first, so a missing one stops it with `PRECONDITION` before any host changes;
decides and creates the data volume; and has the project CA sign the server's
certificate. `cloudfall-engine playbook run services` runs the same playbook
with the same passwords, but creates no volume and signs no certificate, so it
converges only a server that already has both:

```console
uv run cloudfall-engine inventory render --output tmp/ansible-inventory.json
uv run cloudfall-engine playbook run services --inventory tmp/ansible-inventory.json
```

The converge enforces the baseline's declared firewall with the services'
rules before it converges any service, so neither a new replica nor a role
change needs a baseline converge.

## 4. Add a replica

Give the primary a `replication` block and declare the replica as another
`Service` on another server:

```yaml
# mariadb-main, on h1
mariadb:
  version: 11.8.9
  innodbBufferPoolMb: 4096
  replication:
    role: primary
    serverId: 1
    listenAddress: 10.0.0.2
  databases: [...]
```

```yaml
# mariadb-replica, on h2
mariadb:
  version: 11.8.9
  innodbBufferPoolMb: 4096
  replication:
    role: replica
    serverId: 2
    primary: mariadb-main
```

| Field | Meaning |
| --- | --- |
| `serverId` | `server_id`, 1 to 4294967295, unique among the project's MariaDB services (`service_mariadb_server_id_duplicate`) |
| `listenAddress` | Primary only: the one address of its server replicas connect to, never a wildcard (`service_mariadb_replication_listen`) |
| `primary` | Replica only: a MariaDB service of the same environment with `role: primary`, on another server (`service_mariadb_replication_primary`) |

A replica declares no `databases` (`service_mariadb_replica_databases`): its
databases and users are the primary's and arrive by replication. Its `version`
is of the primary's series and not older (`service_mariadb_replica_version`),
so a patch release goes to the replica first.

Run `uv run cloudfall secrets generate` to store the primary's `cloudfall_repl`
password (`REPLICATION_PASSWORD` at `/services/<primary id>`). The primary gets
only its hash. The account connects only over TLS, from the replica servers'
IPv4 only, and the primary's firewall admits its port from those addresses
only. A replica gets the password in a `0600` file for the run only and
verifies the primary's certificate against the project CA.

Then converge the services. An empty replica that never replicated is
**seeded**: one consistent dump of the primary's databases at a recorded GTID
position, loaded, and replication started by GTID from there. The converge
waits until both replication threads run and the replica has applied the
primary's position, then compares the number of tables of every database on
both servers. The answer's `mariadb_replication` names the outcome:

| `outcome` | What happened |
| --- | --- |
| `seed` | The empty replica was loaded from its primary; `reason` names the GTID it was seeded at |
| `attach` | A replica that already holds data whose GTIDs the primary has all seen follows from its own position, with no new copy |
| `promote` | A replica was promoted (section 6) |
| `demote`, `wait-promotion`, `wait-primary` | A role change waits; `reason` says why. Converge again (section 6) |

A replica that already follows its primary is not listed; its link is started
again when it was stopped. A second converge of a pair never seeds again.

## 5. Backups and the restore drill

Every MariaDB service, a replica included, has a `backup` block, so each server
dumps on its own `onCalendar`. On a replica the dumps cover every database it
holds that is not MariaDB's own, read when the dump runs; on a standalone
server or a primary, the declared `databases`. Put the restore drill on the
replica to keep its load off the primary:

```console
uv run cloudfall backup run mariadb-replica
uv run cloudfall backup verify mariadb-replica
```

`cloudfall-mariadb-backup` writes one gzip dump per database into
`backup.directory`, each from one consistent snapshot taken without locking
the tables, and beside it a count file with every table and its rows, counted
from the dump itself. A table of another engine than InnoDB is outside the
snapshot, and the run names it. Users and grants are not dumped: converge
recreates the declared ones. Dumps older than `retentionDays` are deleted,
never a database's newest.

`cloudfall-mariadb-restore-check` loads the newest dump of every database into
the scratch database `cloudfall-restore-check`, compares the table list and
each row count with the count file, and drops the scratch database. It fails
when a database has no dump and on any mismatch, and a row written after the
dump never fails it. It writes nothing to the binary log, so the scratch
database never reaches a replica, and it runs on a read-only replica as
`root`. A replica that holds no database, or a server that declares none, has
nothing to back up and fails both.

Both commands write a receipt, as for PostgreSQL.

## 6. Change roles

A role change is one change to the declaration and a services converge of
every server of the pair, in whatever order the hosts run, with no baseline
step. When one server must wait for the other, the converge answers a wait
with its `reason` and changes nothing that would lose data; the next converge
finishes it. A role change is done after at most two converges. Before the
first, run `uv run cloudfall secrets generate`: the new primary needs a
replication password of its own.

### A planned switch

1. Stop the writers
2. In one change, declare the old replica `role: primary` with a
   `listenAddress`, and the old primary `role: replica` of it, without its
   `listenAddress`. Move `databases` to the new primary, since a replica
   declares none
3. Validate, generate the new primary's password, and converge the services
4. Converge again when the answer lists a wait, until it lists `promote` for
   the new primary and `attach` for the old one

What each server answers depends on which converges first:

- The old primary, declared a replica of a server that still follows it,
  turns read-only and answers `demote`
- The old replica, declared primary while its source is still writable but
  declared its replica, turns read-only and answers `wait-promotion`
- Once its source is read-only and it has applied the source's position (it
  waits up to 60 seconds to reach it), the old replica is **promoted**:
  replication stopped and forgotten, the server writable. The answer names the
  `last_applied_gtid`
- The old primary attaches to the new one. When the new primary has not
  created its replication account yet and converges later in the same run, it
  answers `wait-primary` and attaches in the next converge

### A promotion after the primary is lost

Keep the lost primary's `Service` declared and declare it the replica of the
promoted one, which is also how it comes back: a source that is still
declared is read as one, while an undeclared one that the replica's server
can't reach at all is refused below. Declare the replica `role: primary` with
a `listenAddress` and move `databases` to it (or remove its `replication` to
make it standalone). Take the server's IPv4 as `listenAddress`: it is already
on the server's certificate, and the command below signs none. Generate its
password, then converge the promoted server alone, since a run stops at a
server it can't reach:

```console
uv run cloudfall secrets generate
uv run cloudfall-engine inventory render --output tmp/ansible-inventory.json
uv run cloudfall-engine playbook run services --inventory tmp/ansible-inventory.json --limit h2
```

The converge reads the source on its own server first. A source that can't be
reached, or whose MariaDB does not answer, promotes the replica, and the
answer says `promote` with the reason `its source can not be reached` and the
last GTID the replica applied. It first applies what it already received;
whatever the lost primary wrote that never reached the replica is lost, and
that GTID is what it holds.

### What stops a role change

A refusal stops the converge with `PRECONDITION`, the code in
`error.context.code`, and the service, its server and both GTID positions
(`sourcePosition`, `replicaPosition`) in `error.context`. Nothing changed, and
a replica still replicates.

| Code | When | What to do |
| --- | --- | --- |
| `service_mariadb_promotion_unsafe` | The replica's source is running and writable, or read-only while the replica has not applied its position | Stop the writers and make the source read-only (declare it the replica of the promoted one), or stop it; then converge again |
| `service_mariadb_promotion_unsafe` | The source is no declared service and its link runs, its last error came from a running source, or its port answers | Stop the source, then converge again |
| `service_mariadb_promotion_unsafe` | The source is no declared service and the replica's server can't tell whether it is stopped: the probe timed out, found no route, or could not resolve the name | Let the replica's server reach the source's port, or, once the source is stopped, remove the link by hand (`RESET SLAVE ALL`) |
| `service_mariadb_replica_unattachable` | A replica can't reach its primary, its primary is itself a replica of another server, it holds databases and no GTID history, or it already replicates from another source | Converge or promote the primary first. Cloudfall never moves a replica to another source, and never seeds over a server that holds data with no history: recreating its volume is your step |
| `service_mariadb_replica_diverged` | A former primary holds GTIDs its new primary never saw | See section 7 |

A source that is no declared service is tried from the replica's own server,
which the source admitted while the replica followed it, never from the
controller: a firewall that rejects everyone but the replica would answer the
controller with the same refusal a stopped server gives.

## 7. Bring a former primary back

Declare the former primary `role: replica` of the server promoted over it (it
already is, after section 6) and converge the services once it is up. When its
GTIDs are all ones the new primary has seen, it **attaches** and catches up
from its own position, with no new copy.

When writes reached it that the new primary never saw, the converge stops with
`service_mariadb_replica_diverged` and names those GTIDs: it is not attached
and not emptied. Its data is yours to inspect. Recreating its data volume is
then your explicit step; the empty server is seeded on the next converge like
any new replica.

## 8. Move in from a server Cloudfall does not manage

Not built: a replica declared with `source` validates, but the MariaDB role
refuses it before it changes the server, saying this release validates such a
replica and does not converge it yet. Converging it, and the checks of the
source listed below, wait on a later release.

The declaration names the source instead of a `primary`:

```yaml
mariadb:
  version: 11.8.9
  innodbBufferPoolMb: 8192
  replication:
    role: replica
    serverId: 12
    source:
      host: 10.0.0.11
      port: 3306
      credentialsRef: { environment: production, path: /services/mariadb-main, key: SOURCE_REPLICATION }
      caCertificateFile: pki/old-db-ca.pem
```

`credentialsRef` names one key whose value is `<username>:<password>` on one
line. `caCertificateFile` is a path inside the project to the certificate of
the CA that signed the source's certificate; one that is missing, outside the
project or no CA certificate is refused (`service_mariadb_replication_source`).
Without it the link is not encrypted, and `config validate` warns
`MARIADB_REPLICATION_UNENCRYPTED`. `port` defaults to 3306.

Cloudfall changes nothing on the source. Prepare it yourself:

- **The binary log**: on, row-based (`binlog_format = ROW`), with a
  `server_id` other than the replica's `serverId`, and kept long enough for
  the replica to catch up after its seed
- **The account**: one account that may replicate and read every database,
  connecting from the replica server's IPv4 and, with a CA file, only over
  TLS. On a MariaDB source, Cloudfall's own `cloudfall_repl` holds these
  rights for the same seed and link:

  ```sql
  CREATE USER 'cloudfall_seed'@'203.0.113.20' IDENTIFIED BY '<password>' REQUIRE SSL;
  GRANT REPLICATION SLAVE, BINLOG MONITOR, SELECT, SHOW VIEW, TRIGGER, EVENT, RELOAD
    ON *.* TO 'cloudfall_seed'@'203.0.113.20';
  ```

- **The network**: admit the replica server's IPv4 on the source's port in
  the source's firewall
- **The version**: a source no newer than the replica's `version`

Per spec 019, once built, the converge checks the source before installing
anything, seeds the replica with the source's databases but not its users,
and follows it until you promote the replica as in section 6, then declare
`databases` for what it holds.

## Audit

`cloudfall audit` reads each MariaDB server from its observation:

| Check | Drift when |
| --- | --- |
| `mariadb.version[<id>]` | The installed package is not `version` |
| `mariadb.series[<id>]` | The series reached its end of life |
| `mariadb.unit[<id>]` | `mariadb.service` is not enabled and running |
| `mariadb.configuration[<id>]` | `/etc/mysql/mariadb.conf.d/90-cloudfall.cnf` differs from the rendered one |
| `mariadb.role[<id>]` | A replica is writable or its replication threads stopped, or a standalone server or a primary replicates from somewhere |
| `mariadb.lag[<id>]` | A replica is more than five minutes behind |
| `mariadb.accounts[<id>]` | A declared database or user is missing or locked |

Your own server settings go in a file of their own in
`/etc/mysql/mariadb.conf.d/` that sorts before `90-cloudfall.cnf`; only the
managed file is compared.

```console
uv run cloudfall-engine inventory render --output tmp/ansible-inventory.json
uv run cloudfall-engine playbook run inspect --inventory tmp/ansible-inventory.json --extra-vars cloudfall_inspect_output_directory=$PWD/tmp/observed
uv run cloudfall audit --observed tmp/observed
```
