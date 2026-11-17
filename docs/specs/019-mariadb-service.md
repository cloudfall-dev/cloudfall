# S-019: MariaDB service

status: approved
serves: G-1, G-3

## Problem

A `Service` is PostgreSQL, Redis or Elasticsearch (`serviceKind` in
`config/schemas/v1/service.schema.json`, `ServiceKind` in `sdk/src/cloudfall/domain.py`). A
team whose application stores its data in MariaDB runs that part by hand: the package and its
pin, the data volume, the buffer pool, one user per application, the replication link to a
second server, dumps, and the check that a dump restores. None of it is declared, so
`config validate`, `audit`, `backup run` and `backup verify` know nothing about it, and an
agent has no operation to propose for it. ROADMAP.md and ARCHITECTURE.md name MySQL as
catalog breadth that follows the PostgreSQL and Redis pattern.

MariaDB adds one thing that pattern does not have: a second copy that follows the first.
S-006 left replication out of scope for PostgreSQL; here it is part of the kind, because a
replica is also how a database that already runs elsewhere moves in without a long write stop.

## Behaviour

### Declaration

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

`spec.mariadb` is required for the kind and refused for the others:

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | Exact `X.Y.Z` of a supported series (below); installed from MariaDB's APT archive of that exact release (the series repository keeps only the newest patches) and held (`apt-mark hold`), so an unattended upgrade never restarts the server |
| `innodbBufferPoolMb` | yes | `innodb_buffer_pool_size`, 128 or more |
| `databases` | no | The databases and their users, in the two forms PostgreSQL has (S-006): `application` or `owner` with `passwordRef`. An `owner` entry may add `authentication: ed25519` (below) |
| `remoteAccess` | no | `listenAddress` and `allowedSources`, the shape PostgreSQL uses (S-006) |
| `replication` | no | The server's role in a pair (below). Absent means a standalone server that writes no binary log |

One server holds at most one MariaDB service: the Debian package runs one instance per host.

### Supported versions

Cloudfall installs only long-term series MariaDB still maintains: `10.11`, `11.4`, `11.8`
and `12.3` at the time of writing. Rolling releases are never accepted: each ends within
months. The accepted series live in one table in the SDK with the end-of-life date of each,
the date MariaDB's maintenance policy gives for the end of community binaries, taken when a
Cloudfall release is cut, and follow the rule S-012 set for Elasticsearch:

- any other version fails validation; there is no flag that allows it
- within 90 days of a series' date, `config validate` warns `MARIADB_VERSION_ENDING` and
  names the date
- after the date, the warning stays, and `audit` reports the service as running an
  unsupported version
- the series leaves the table in the next minor release of Cloudfall, with a changelog entry

MariaDB publishes packages only for series newer than the one a Debian release ships, so
`10.11` and `11.4` exist for Debian 12 and not for Debian 13. A `version` with no package for
the server's Debian release fails the package requirement (S-011) before anything is
installed.

A change of `version` inside a series installs the package, restarts the server once and
runs `mariadb-upgrade`. A `version` whose series differs from the installed one is refused by
the role before anything changes.

### What the server needs

The kind yields S-011 requirements, so nothing below is prepared by hand:

| Requirement | Value |
|---|---|
| `package` | `mariadb-server` at `version` from the repository of its series |
| `volume` | `mariadb`, 10G unless overridden, `mirrored`, mounted at `/var/lib/mysql`, needed by `mariadb.service` |
| `port` | with `remoteAccess`, `bind.port` from `allowedSources` only; on a primary, `bind.port` from the servers of its replicas only |
| `certificate` | with `remoteAccess` or `replication`, one server certificate from the project CA, names: the server's hostname and addresses |
| `secret` | none on the host: the password of the replication account is generated on the controller (`cloudfall secrets generate`); the primary holds only its hash, as D-9 has it for owners |

The role writes the managed settings to one file, `/etc/mysql/mariadb.conf.d/90-cloudfall.cnf`:
the listener, the buffer pool, `utf8mb4` as the server character set, the TLS files and the
replication block. Other settings are the operator's, in a file of their own that sorts
before it; `audit` compares only the managed file. A `Baseline` declares no memory, so the
role is where the buffer pool meets the host: it refuses, before changing anything, an
`innodbBufferPoolMb` above three quarters of the server's memory.

### Access

- **Cloudfall's own account** is `root` through the unix socket, as Debian sets it up: no
  password exists for it. The role, the backup scripts and the drill run as the Linux `root`
- **A database with `application`** belongs to a user named after the application's Linux
  user, authenticated by the unix socket only: the process of that Linux user connects with
  no password, and nobody connects as it over TCP. This is PostgreSQL's peer form
- **A database with `owner`** belongs to a user of that name whose password comes from
  `passwordRef`. The controller computes the password hash and only the hash reaches the host
  and the server, following the rule D-9 set for PostgreSQL: the plaintext is never staged on
  a host or sent in a statement. The password is set on every converge, so a rotation is a
  commit in the secrets repository and a services converge
- The hash is `mysql_native_password` unless the entry says `authentication: ed25519`. The
  first is the one form every client library speaks as shipped; the second is the stronger
  one, for applications whose client supports it, and the guide lists which do
- Each user has every privilege on its own database and none elsewhere
- A database or user that Cloudfall created and that is no longer declared is not dropped:
  the user is locked, and `audit` lists both as undeclared. Users and databases Cloudfall did
  not create are left alone and listed as unmanaged. Dropping data is never a side effect of
  a converge

With `remoteAccess`, the server also listens on `listenAddress`. An `owner` connects from an
allowed source only over TLS, with the server's certificate from the project CA; a plain
connection from outside the host is refused. `application` users stay socket-only. The
nftables rules are the ones S-006 renders. Without `remoteAccess` and without `replication`,
the server listens on `bind.address` only.

### Replication

A service is one server in one of three states: standalone (no `replication`), `primary`, or
`replica`. There is no cluster and no automatic failover: a role changes only when the
declaration changes and a converge runs.

The primary:

```yaml
mariadb:
  version: 11.8.9
  innodbBufferPoolMb: 4096
  replication:
    role: primary
    serverId: 1
    listenAddress: 10.0.0.2
  databases: [...]
```

Its replica, another `Service` on another server:

```yaml
mariadb:
  version: 11.8.9
  innodbBufferPoolMb: 4096
  replication:
    role: replica
    serverId: 2
    primary: mariadb-main
```

| Field | Meaning |
|---|---|
| `role` | `primary` or `replica` |
| `serverId` | `server_id`, 1 to 4294967295, unique among the MariaDB services of the project |
| `listenAddress` | Primary only: the address replicas connect to, one address of the server, never a wildcard. When `remoteAccess` is also declared, the server listens on both addresses |
| `primary` | Replica only: the id of a MariaDB `Service` of the same environment whose role is `primary` |
| `source` | Replica only, instead of `primary`: a server Cloudfall does not manage (below) |

**A primary** writes a row-based binary log with GTIDs in strict mode and keeps it for seven
days. It has one replication account, `cloudfall_repl`, whose generated password is stored
sops-encrypted; the account connects only over TLS and only from the addresses of the
servers of the services that name this primary, and the firewall admits `bind.port` from
those addresses only. It may read every database, because it also seeds a replica.

**A replica** is read-only for every user but `root`, and replicates by GTID from its
position. It verifies the primary's certificate against the project CA. It declares no
`databases`: its databases and users are the primary's, and arrive by replication. Its
`version` is of the same series as the primary's and not older, so a patch change goes to the
replica first. A replica holds the replication password, because the server needs the
plaintext to log in to the primary: it is staged in a `0600` file for the run only, handed
to the server in a session that writes no statement log, and kept afterwards only where
MariaDB keeps it. It appears in no rendered inventory, playbook output, command answer or
receipt.

**Seeding.** A replica that holds no database of its own and has never replicated is seeded
by the converge: the role takes one consistent dump of every non-system database from the
source at a recorded GTID position, loads it, and starts replication from that position. It
then waits until both replication threads run and the replica has reached the position the
source had when the load ended, and compares the table counts of every database with the
source. A server that already holds a database is never seeded and never emptied; if it
cannot attach (next section), the converge stops and says so.

**Promotion.** Changing a replica to `role: primary`, or removing its `replication`, promotes
it: replication is stopped and forgotten, and the server becomes writable. Because a
promotion drops whatever the replica had not yet applied, the role first reads the source:

- the source can't be reached: the replica is promoted, and the answer names the last GTID
  it applied
- the source is read-only and the replica has applied its position: the replica is promoted
- otherwise the converge stops with `PRECONDITION` and names both positions, and nothing
  changes

"Can't be reached" is read from the replica's side. A source that is a declared service is
read on its own server; one that is not is tried from the replica's server, which the source
admitted while the replica followed it, and counts as stopped only when that connection is
refused and the replica's last error is not one only a running source sends. A refusal the
controller reads proves nothing, since a firewall that rejects everyone but the replica
answers the same; a timeout, no route or a name the replica can't resolve leaves the source
unknown, and the converge stops with `PRECONDITION`.

So a planned switch is: stop the writers and make the old primary read-only (for a Cloudfall
primary, by declaring it the replica of the new one in the same change), then converge.
ARCHITECTURE.md already says an operator initiates every promotion; this is the form it
takes.

**A former primary comes back as a replica** without a new copy when its GTID position is
one the new primary's binary log still holds: it attaches and catches up. When it is not
(writes reached it that the new primary never saw, or the log has expired), the converge
stops and reports it. Recreating the volume is then the operator's explicit step, after which
the empty server is seeded like any other.

### A replica of a server Cloudfall does not manage

A team that already runs MariaDB elsewhere moves in by declaring a replica of it, letting it
catch up, and promoting it. The link replicates by MariaDB GTID, so a MySQL server is not
supported as the `source` for now; a later spec may add a link that does not use GTID:

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

- `credentialsRef` names one key of the project's store whose value is
  `<username>:<password>` on one line: an account the team creates on the source with the
  rights to replicate and to read every database. Nothing on the source is changed with it
- `caCertificateFile` is a path inside the project to the certificate (never the key) of the
  CA that signed the source's certificate. Without it the link is not encrypted, and
  `config validate` warns `MARIADB_REPLICATION_UNENCRYPTED`
- Before installing anything the role logs in to the source and refuses when it writes no
  binary log, when its binary log is not row-based, when it runs a version newer than
  `version`, when its `server_id` equals `serverId`, or when the account lacks a right the
  seed or the link needs, naming what is missing
- The seed copies the non-system databases. The source's users are not copied, and
  statements that create or change users on the source are not applied on the replica: the
  replica's accounts are Cloudfall's
- After the promotion the team adds `databases` for what the server now holds. A declared
  database that exists is adopted as it is, and its user is created or given the declared
  password

The service guide gives the steps on the source, which Cloudfall does not manage: the
account, the binary log settings, and admitting the replica's address.

### Backups and the restore drill

`backup` keeps its contract: a schedule, a retention, an optional drill, receipts from
`cloudfall backup run` and `cloudfall backup verify`. It works the same on a standalone
server, a primary and a replica, so a team can take its backups on the replica.

- `cloudfall-mariadb-backup` writes one compressed dump per database of the server, each
  from one consistent snapshot without locking the tables, with the GTID position in it, and
  deletes dumps older than `retentionDays`. On a server with declared `databases` those are
  the databases; on a replica they are every non-system database it holds. Users and grants
  are not in the dumps: converge recreates the declared ones
- Each dump is written with the row count of every table, read in the snapshot the dump
  was taken from, in a file beside it
- `cloudfall-mariadb-restore-check` loads the newest dump of every database into a scratch
  database, compares the table list and each row count with that file, and drops the scratch
  database. It fails when a database has no dump and on any mismatch
- The drill writes nothing to the binary log, so a scratch database never reaches a replica,
  and it runs on a read-only replica as `root`

### Validation

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- `mariadb` on another kind, or its absence on this one
- `version` that is not `X.Y.Z` of a supported series (`service_mariadb_version`)
- a server that holds two MariaDB services (`service_mariadb_server_shared`)
- a database entry outside the S-006 rules: both `application` and `owner` or neither,
  `owner` without `passwordRef`, `passwordRef` next to `application`
- an `owner` or database named `root`, `mysql`, `mariadb.sys`, `cloudfall_repl`, the metrics
  user, a system database, or the Linux user of an `Application` with a database on the same
  service (`service_database_owner_reserved`)
- two services with the same `serverId` (`service_mariadb_server_id_duplicate`)
- a primary without `listenAddress`, or a replica with one (`service_mariadb_replication_listen`)
- a replica with both `primary` and `source` or with neither, with a `primary` that is not a
  MariaDB service of the same environment with `role: primary`, that is itself, or that is
  on the same server (`service_mariadb_replication_primary`)
- a replica that declares `databases` (`service_mariadb_replica_databases`)
- a replica whose `version` is of another series than its primary's, or older
  (`service_mariadb_replica_version`)
- a primary with a replica, or a replica, whose baseline declares no firewall
  (`service_mariadb_replication_unfirewalled`), or a replica server with no address the
  primary can admit (`service_mariadb_replica_unaddressed`)
- a `caCertificateFile` that is missing, outside the project or not a CA certificate
  (`service_mariadb_replication_source`)
- a server whose baseline declares no storage pool, several, or one without a secondary
  physical volume: the S-014 codes
- `remoteAccess` under the S-006 rules; on a replica it needs no `owner` database of its own
- an IPv6 network in `remoteAccess.allowedSources`, such as `2001:db8::/64`
  (`service_mariadb_remote_access_ipv6`): a MariaDB account host has no netmask form for
  IPv6, so the message names the source and says a single IPv6 address (`/128`) or an IPv4
  network is accepted. This rule is MariaDB's only; PostgreSQL keeps S-006's (D-26)

### Audit and metrics

- `audit` reports drift when the installed version differs from `version`, when
  `mariadb.service` is not enabled and running, when the managed file differs from the
  rendered one, when a listener is on an address the declaration does not name, when a
  requirement of the server is unmet (S-011), and when the server's role is not the declared
  one: a replica that is writable or whose replication threads are stopped, a standalone
  server or a primary that replicates from somewhere
- For a replica it also reports the lag in seconds, as a finding above five minutes
- With `metrics.enabled`, the collector on the server scrapes it with Alloy's MySQL exporter
  as a user of its own that can read status and replication state only, with the service id as the
  label the other exporters use. The services dashboard (S-016) gains MariaDB panels,
  replication lag among them

### Documentation

`docs/mariadb-service-guide.md` covers declaring a standalone server, adding a replica,
taking backups on the replica, a planned switch and a promotion after the primary is lost,
bringing a former primary back, and moving in from a server Cloudfall does not manage.

Touches `config/schemas/v1/service.schema.json`, `config/examples/services/`,
`sdk/src/cloudfall/domain.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/requirements.py`,
`sdk/src/cloudfall/secrets.py`, `sdk/src/cloudfall/audit.py`,
`engine/src/cloudfall_engine/ansible_inventory.py`, a new role
`engine/ansible/roles/cloudfall_mariadb`, `engine/ansible/playbooks/services.yml`,
`engine/ansible/playbooks/backup.yml`,
`engine/ansible/roles/cloudfall_logging_collector/templates/config.alloy.j2`,
`engine/ansible/roles/cloudfall_logging_backend/files/dashboards/cloudfall-services.json`,
ROADMAP.md, ARCHITECTURE.md, and the guide.

## Acceptance criteria

- S-019-1: A project with one `Server`, its baseline, and the declaration above passes
  `cloudfall config validate` with exit 0, and `cloudfall-engine inventory render` carries the
  service with its databases and no password value
- S-019-2: For the project in `config/examples` without a MariaDB service, the rendered
  inventory is byte-identical before and after the change
- S-019-3: After `services.yml` converges S-019-1 on a fresh Debian 13 host, `SELECT
  VERSION()` as `root` over the socket reports the declared version, the package is held,
  `innodb_buffer_pool_size` is the declared one, the data directory is on the `mariadb`
  volume, no listener for 3306 exists on a non-loopback address, and the binary log is off
- S-019-4: A second converge changes nothing (`changed=0`) and does not restart the server;
  an `innodbBufferPoolMb` above three quarters of the host's memory stops the converge before
  any task changes the host
- S-019-5: The Linux user of application `crm` connects through the socket with no password
  and can create a table in `crm`; it is refused on `billing`, and nobody connects as it
  over TCP. `billing` connects on loopback with its password, can create a table in
  `billing`, and is refused on `crm`
- S-019-6: The password of `billing` and of `cloudfall_repl` appears in none of the rendered
  inventory, playbook output, command answers or receipts; the file staged for `billing`
  holds a hash and never the password; a `passwordRef` whose file or key is missing stops
  the converge with `PRECONDITION` before any playbook runs
- S-019-7: Removing a declared database and converging leaves the database and its rows in
  place, locks its user, and `audit` lists both as undeclared; a user created by hand is
  left alone and listed as unmanaged
- S-019-8: `cloudfall backup run mariadb-main` writes one dump and one count file per
  database and a `BackupReceipt`; `cloudfall backup verify mariadb-main` restores each into a
  scratch database, finds equal tables and row counts, drops the scratch database and writes
  its receipt; with no dump it fails and says so; a row written to the source after the dump
  does not fail the drill
- S-019-9: Dumps older than `retentionDays` are gone after the next backup run, and the
  newest is kept
- S-019-10: Each refusal listed under Validation fails `cloudfall config validate` with
  `CONFIG_INVALID` and its code; a version of a series within 90 days of its end-of-life date
  validates with the warning `MARIADB_VERSION_ENDING` naming the date
- S-019-11: With `remoteAccess` of `listenAddress: 10.0.0.2` and `allowedSources:
  [10.0.0.0/24]`, `billing` connects from an allowed address over TLS verifying the project
  CA, a plain connection from that address is refused, and the ruleset holds `ip saddr
  10.0.0.0/24 tcp dport 3306 accept` and no unrestricted rule for 3306
- S-019-12: Converging a primary and an empty replica on two hosts gives a replica with
  every database and row of the primary, both replication threads running by GTID, and
  `read_only` on; a row written on the primary afterwards appears on the replica; a write on
  the replica as `billing` is refused; the primary's ruleset admits 3306 from the replica's
  addresses only, and the link is encrypted and verified against the project CA
- S-019-13: A second converge of the pair changes nothing and does not seed again; a replica
  that already holds a database and cannot attach stops the converge with no database
  dropped
- S-019-14: With the primary stopped, changing the replica to `role: primary` and converging
  makes it writable with no replication configured, and the answer names the last GTID
  applied. With the primary running and writable, the same change stops with `PRECONDITION`
  naming both positions, and the replica still replicates
- S-019-15: Declaring the old primary the replica of the new one and the old replica the
  primary, in one change with no writer running, ends after at most two converges with the
  roles swapped, no row lost and no seed
- S-019-16: A former primary that took a write the new primary never saw is not attached and
  not emptied: the converge stops and reports the divergence
- S-019-17: With a MariaDB server of an older supported series that Cloudfall did not
  create, a replica declared with `source` is seeded and follows it; a user created on the
  source afterwards does not appear on the replica; after promotion, declaring one of its
  databases with an `owner` adopts the database with its rows and gives the owner the
  declared password
- S-019-18: The converge of S-019-17 stops before installing the package when the source
  writes no binary log, when its binary log is not row-based, when it is newer than
  `version`, and when the account lacks a needed right; the source's password appears in no
  playbook output, command answer or receipt, and nothing on the source is changed
- S-019-19: `cloudfall audit` reports drift when the installed version differs from the
  declaration, when the unit is stopped, when the managed file is changed by hand, when a
  replica is made writable by hand, and when its replication is stopped
- S-019-20: With `metrics.enabled`, the rendered collector configuration holds one MySQL
  exporter for the service, labelled with the service id, and none without it; the services
  dashboard shows replication lag for a replica
- S-019-21: An `owner` entry with `authentication: ed25519` gets a user of that plugin, a
  client that supports it logs in with the password, and the staged file holds the ed25519
  hash

## Out of scope

- Galera, and any cluster of more than one writable server
- Automatic failover and automatic promotion
- More than one source per replica, and a replica of a replica
- A replica outside Cloudfall of a primary Cloudfall manages
- Semi-synchronous replication
- Changing the series of an installed server (10.11 to 11.4); moving up is a new server
  seeded as a replica of the old one, then promoted
- MySQL and Percona Server as a kind: a separate `serviceKind` with its own packages
- A MySQL server as the `source` of a move: the link replicates by MariaDB GTID, which MySQL
  does not write; a later spec may add a link that does not use GTID
- Physical backups (`mariadb-backup`) and point-in-time recovery from the binary log
- Connection pooling and proxies (ProxySQL, MaxScale)
- A pass-through of server settings in the declaration
- Connections between components and stores with a generated user per connection:
  cloudfall-dev/cloudfall#216
- Dropping a database or a user

## Decisions relied on

- D-9: an owner password reaches the server only as a hash computed on the controller;
  stated for PostgreSQL, applied here to MariaDB owners
- D-26: `remoteAccess.allowedSources` of a MariaDB service refuses an IPv6 network at
  validation; a single IPv6 address and IPv4 networks stay accepted

## Issues

- cloudfall-dev/cloudfall#419: S-019-1, S-019-2, S-019-10
- cloudfall-dev/cloudfall#420: S-019-3, S-019-4, S-019-5, S-019-6, S-019-7, S-019-21
- cloudfall-dev/cloudfall#421: S-019-11
- cloudfall-dev/cloudfall#422: S-019-8, S-019-9
- cloudfall-dev/cloudfall#423: S-019-12, S-019-13, S-019-14, S-019-15, S-019-16
- cloudfall-dev/cloudfall#424: S-019-17, S-019-18
- cloudfall-dev/cloudfall#425: S-019-19, S-019-20

## Verification

No criterion is verified yet.

- S-019-17, S-019-18: the in-house client that reads a source before anything is installed
  (`cloudfall_mariadb_source`) is proven against a stand-in only; it needs a live proving run
  against MariaDB 10.11 and 11.x before the stable release
