# S-022: ClickHouse service

status: draft
serves: G-1, G-3

## Problem

A `Service` is PostgreSQL, Redis, Elasticsearch or MariaDB (`serviceKind` in
`config/schemas/v1/service.schema.json`, `ServiceKind` in `sdk/src/cloudfall/domain.py`). A
team whose application keeps its events and analytics in ClickHouse runs that part by hand:
the packages and their pin, the data volume, the memory limit, ClickHouse Keeper and its
quorum, TLS between the servers, one user per database, the second copy of the data, backups,
and the check that a backup restores. None of it is declared, so `config validate`, `audit`,
`backup run` and `backup verify` know nothing about it, and an agent has no operation to
propose for it. ROADMAP.md and ARCHITECTURE.md do not name ClickHouse; this spec adds it to
the catalog breadth that follows the PostgreSQL and Redis pattern.

ClickHouse differs from the kinds before it in two ways:

- Two replicas are equals. Both take writes and both answer reads, so there is no primary to
  declare, no promotion and no failover (S-019, S-020). What can be lost instead is the
  coordination: a replicated table turns read-only when Keeper has no quorum
- The copy is not made by the server alone. It needs Keeper, a quorum of three voters, and so
  a third server that holds no data

## Behaviour

### Declaration

```yaml
apiVersion: cloudfall/v1
kind: Service
metadata:
  id: clickhouse-main
  description: Product events and reports
spec:
  serviceKind: clickhouse
  environment: production
  server: h1
  bind:
    address: 127.0.0.1
    port: 9000
  clickhouse:
    version: 26.8.37.168
    maxServerMemoryMb: 16384
    replicas: [h1, h2]
    keeper:
      servers: [h1, h2, h3]
    databases:
      - name: events
        owner: events
        passwordRef: { environment: production, path: /services/clickhouse-main, key: EVENTS_PASSWORD }
    clientAccess:
      allowedSources: [10.0.0.0/24]
  backup:
    directory: /var/backups/cloudfall/clickhouse-main
    onCalendar: "*-*-* 02:40:00 UTC"
    restoreCheckOnCalendar: "Sun *-*-* 05:30:00 UTC"
    retentionDays: 14
  metrics:
    enabled: true
```

`spec.clickhouse` is required for the kind and refused for the others:

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | Exact `X.Y.Z.N` of a supported series (below); every ClickHouse package on every server of the service is installed at that version from the `lts` suite of ClickHouse's APT repository, which keeps every earlier build, and held (`apt-mark hold`), so an unattended upgrade never restarts a server |
| `maxServerMemoryMb` | yes | `max_server_memory_usage` of each replica, 2048 or more |
| `replicas` | no | The servers that hold the data, one or two; must contain `spec.server`. Absent means `[spec.server]` |
| `keeper.servers` | no | The servers that vote in Keeper, one or three; must contain every replica. Absent means `replicas`, which is valid only for one replica |
| `databases` | no | The databases and their owners: `name`, `owner`, `passwordRef` (the S-006 owner form) |
| `clientAccess` | no | `allowedSources`: the addresses that may connect to every replica from outside its server (below) |

`remoteAccess` (S-006) is refused for this kind: it names one listen address, and here every
replica listens on its own.

### Supported versions

Cloudfall installs only the long-term support (LTS) series ClickHouse still maintains:
`26.3` and `26.8` at the time of writing. ClickHouse publishes two a year and maintains each
for one year, so two are accepted at any time. Monthly stable releases are never accepted:
only the three latest are maintained. The accepted series live in one table in the SDK with
the end-of-support date of each. ClickHouse publishes the rule and no date, so the date is
the first release of the series plus one year (`26.3`: 2027-03-27, `26.8`: 2027-08-30),
checked against the supported list in ClickHouse's `SECURITY.md` when a Cloudfall release is
cut. The table follows the rule S-012 set for Elasticsearch:

- any other version fails validation; there is no flag that allows it
- within 90 days of a series' date, `config validate` warns `CLICKHOUSE_VERSION_ENDING` and
  names the date
- after the date, the warning stays, and `audit` reports the service as running an
  unsupported version
- the series leaves the table in the next minor release of Cloudfall, with a changelog entry

ClickHouse publishes patch builds of an LTS series almost daily. `version` is exact, so
a team moves when it changes the declaration, and nothing follows the series by itself.

Because a series lasts one year, moving to the next one is part of the kind and not a new
server. ClickHouse supports replicas of two versions working together while they are less
than a year or one LTS series apart, which is what a rolling change needs:

- a change of `version` inside a series, or to the next accepted series, is a rolling
  change: one server at a time, the server that only votes first, then the replica that is
  not `spec.server`, then `spec.server`
- before a replica is restarted the other replica must be healthy (below), and after it the
  role waits until the restarted one is healthy again before it moves on. With one replica
  the restart is a short stop of the service
- an older `version` than the installed one, and a series that is not the installed one or
  the next accepted one, are refused by the role before anything changes. ClickHouse
  promises no way back once a feature of the newer version was used

### One replica is a pair that waits for its second

The role configures a service with one replica exactly as one with two: Keeper runs, every
declared database is a `Replicated` database, and every table in it is a replicated table.
Growing is adding a server to `replicas`, two more to `keeper.servers`, and converging; no
table is converted and no data is copied by hand.

- The replica name of a server is its server id, and the shard name is the service id
- A declared database is created with the `Replicated` engine, so a `CREATE`, `ALTER` or
  `DROP` an owner runs on one replica is applied on the other with no `ON CLUSTER`.
  ClickHouse's source calls this engine beta since 24.4 and its documentation gives it no
  status; the guide says so
- Statements on a partition that copy or move files by hand (`FREEZE`, `ATTACH`, `FETCH`,
  `DETACH`) run on the replica that received them only: ClickHouse does not replicate them
- An owner can create tables only with a replicated engine in its database (the server
  refuses the others), and a `MergeTree` engine named without the replicated form is
  created as its replicated form. So a table an owner creates always exists on both
  replicas with its rows
- An insert is acknowledged by the replica that took it and reaches the other one
  afterwards. A team that needs it on both before the acknowledgement sets
  `insert_quorum` in its own queries; the guide says what that costs
- `spec.server` is the service's home: the backup and restore-drill timers, the database and
  user converge and `cloudfall backup run` execute there once, not on every replica

### Keeper

Keeper is part of the service and has no declaration of its own.

- On a replica, Keeper runs inside the ClickHouse server process: the `clickhouse-keeper`
  package conflicts with `clickhouse-server`, so one host can't hold both. On a server of
  `keeper.servers` that is not a replica (the third voter), the role installs
  `clickhouse-keeper` at the same `version` and nothing else of ClickHouse; that server
  holds no table data
- ClickHouse recommends dedicated Keeper hosts for production and accepts Keeper beside the
  server where ingestion is low. Three dedicated hosts beside two replicas is five servers,
  which is outside what this kind is for; the guide names the limit, and `audit` reports
  Keeper's own latency so a team sees when it is reached
- One voter is accepted only with one replica. Two replicas need three voters: with two,
  losing either server stops every write, which is worse than having no second replica.
  There is no five-voter form
- The Keeper ensemble serves this service only. Two ClickHouse services never share one,
  and Cloudfall offers Keeper to nothing else
- A change of `keeper.servers` on a running service is applied one voter at a time: a new
  voter joins and catches up before an old one leaves, and the role refuses to start a step
  while the ensemble has no leader. Going from one voter to three is two such steps
- While Keeper has no quorum, every replicated table is read-only and reads still work.
  `audit` reports it, and nothing in a converge turns a table writable by force

### Adding, replacing and losing a replica

- **A second replica** declared on an empty server joins by the converge: the role creates
  each declared database there with the same Keeper path, the server creates the tables and
  fetches their parts from the first replica, and the role waits until the new replica is
  healthy
- **Healthy** means, for every replicated table on the replica: it is not read-only, its
  replication queue holds no entry older than five minutes, and its delay behind the other
  replica is under five minutes. The role waits up to a declared-nowhere default of one hour
  for it and then stops with what is still behind; a later converge resumes the wait
- **A replica whose server is lost** leaves its registration in Keeper, and the other
  replica keeps the log for it. Replacing it is naming the new server in `replicas` instead
  of the old one (and in `keeper.servers`): the converge moves the Keeper vote, removes the
  registration of the server that is no longer declared, and the new server joins as above.
  The registration of a replica that is still declared, or that Keeper sees as active, is
  never removed
- **A declared replica that comes back empty** (same server id, new volume) is detected:
  its data directory holds no table while Keeper holds its registration. The role removes
  that registration and lets it join as a new replica. A replica that holds any table is
  never emptied and never re-registered
- With both replicas lost, the data is in the backups; there is nothing to attach to

### What each server needs

The kind yields S-011 requirements per server, so nothing below is prepared by hand:

| Requirement | Value |
|---|---|
| `package` | on a replica `clickhouse-server` and `clickhouse-client` (with `clickhouse-common-static`) at `version`; on the third voter `clickhouse-keeper` at `version` |
| `volume` | on a replica `clickhouse`, 20G unless overridden, `mirrored` for one replica and `expendable` for two (the copy is on another server), mounted at `/var/lib/clickhouse`, needed by `clickhouse-server.service`; a replica that was alone keeps its mirrored volume when the service grows (D-18). On the third voter `clickhouse-keeper`, 5G, `expendable`, mounted at `/var/lib/clickhouse-keeper` |
| `port` | the replication port from the other replica only; the Keeper client port from the replicas only; the Keeper Raft port from the other voters only; with `clientAccess`, the TLS client ports from `allowedSources` only |
| `certificate` | with two replicas or `clientAccess`, one per server from the project CA, usage server and client, names: the server's hostname and addresses |
| `secret` | `generate`: the password of Cloudfall's own account, and with two replicas the credentials the replicas present to each other |

The ports are ClickHouse's defaults: 9000 (native) and 8123 (HTTP) on loopback, 9440 and
8443 for the same two over TLS, 9010 for replication over TLS, 9281 for Keeper clients over
TLS, 9234 for Raft, 9363 for metrics on loopback. `bind.port` is the loopback native port.

The role writes the managed settings to files of its own in `config.d` and `users.d`
(listeners, memory limit, Keeper and its voters, the replica and shard names, TLS, the
replication credentials, the backup disk, the metrics endpoint). Other settings are the
operator's, in files of their own that sort before them; `audit` compares only the managed
files. A `Baseline` declares no memory, so the role is where the limit meets the host: it
refuses, before changing anything, a `maxServerMemoryMb` above nine tenths of the server's
memory.

Every connection that leaves a server is encrypted and verified against the project CA:
replica to replica, replica to Keeper, and voter to voter. With one replica and no
`clientAccess` nothing listens outside loopback and no certificate is issued.

One server holds at most one ClickHouse replica and votes in at most one Keeper ensemble: the
Debian packages run one instance per host.

### Access

ClickHouse has no login by unix socket, so there is no `application` form (S-006) here:
every account has a password.

- **Cloudfall's own account** is `cloudfall`, defined in a `users.d` file on each replica so
  that it works while Keeper has no quorum. Its password is generated, stored
  sops-encrypted, and lands on each replica as a `0600` file owned by `clickhouse`; the
  role, the backup scripts and the drill read it there. The `users.d` file holds only its
  hash, and the account connects from loopback only. It creates the databases and the
  users, and nothing else in Cloudfall holds that right
- **`default`** is removed: no account exists that logs in with no password
- **A database with `owner`** belongs to a user of that name whose password comes from
  `passwordRef`. The controller computes the password hash and only the hash reaches the
  servers, following the rule D-9 set for PostgreSQL: a salted SHA-256, which every client
  speaks as shipped. Users and their grants are stored in Keeper, so one statement on
  `spec.server` creates the user on both replicas
- Each owner has every privilege on its own database and none elsewhere, and can't create
  users or change settings of the server
- A database or user that Cloudfall created and that is no longer declared is not dropped:
  the user loses its right to log in, and `audit` lists both as undeclared. Users and
  databases Cloudfall did not create are left alone and listed as unmanaged. Dropping data is
  never a side effect of a converge
- A database that Cloudfall did not create with the `Replicated` engine and that is then
  declared is refused: it would look declared and not be copied. Moving its tables is the
  team's step, and the guide gives it

Without `clientAccess`, each replica answers clients on loopback only, in plain text. With
it, each replica also listens for clients on the IPv4 address its `Server` declares, over
TLS only (9440 and 8443), admitted from `allowedSources` by nftables; a plain connection
from outside the host is refused. An application lists both replicas in its client and uses
whichever answers: both take writes, so there is no primary to find (S-021 has no
counterpart here).

### Backups and the restore drill

`backup` keeps its contract: a schedule, a retention, an optional drill, receipts from
`cloudfall backup run` and `cloudfall backup verify`. Both run on `spec.server`.

- `cloudfall-clickhouse-backup` takes one backup of every declared database with
  ClickHouse's own `BACKUP` into `backup.directory`, waits for it, fails unless the server
  reports it as created, and deletes backups older than `retentionDays`. Users and grants
  are not in it: converge recreates the declared ones
- Each backup is written with the row count of every table, taken from the parts the backup
  holds, in a file beside it
- `cloudfall-clickhouse-restore-check` restores the newest backup of every declared database
  under a scratch name, compares the table list and each row count with that file, and
  drops the scratch database. It fails when there is no backup, on any mismatch, and when
  the restore would leave the data volume with less than a tenth of its space
- The scratch database and its tables get Keeper paths of their own, exist on `spec.server`
  only, and are removed from Keeper with the drop; the other replica never creates them
- That holds because a table's Keeper path comes from its identifier. A table an owner
  created with a literal Keeper path would restore onto the path of the live table, so the
  drill refuses it before restoring anything and names it, and `audit` reports such a table
- A backup is a full copy. `backup.directory` is on the server that also holds the data, as
  for the other kinds, so it protects against a dropped table and not against a lost server;
  the second replica and an off-server copy of the directory are what protect against that

### Validation

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- `clickhouse` on another kind, or its absence on this one
- `version` that is not `X.Y.Z.N` of a supported series (`service_clickhouse_version`)
- `replicas` without `spec.server`, with an undeclared server, a duplicate, or more than two
  entries (`service_clickhouse_replicas`)
- `keeper.servers` that misses a replica, names an undeclared server or a duplicate, or
  holds a number of servers other than one for one replica and three for two
  (`service_clickhouse_keeper_servers`)
- a server that is a replica or a voter of two ClickHouse services
  (`service_clickhouse_server_shared`)
- a server of the service in another environment than the service
  (`service_clickhouse_environment`)
- `remoteAccess` on this kind (`service_clickhouse_remote_access`)
- a database without `owner` or `passwordRef`, or with `application`
  (`service_clickhouse_database`)
- an `owner` or database named `default`, `cloudfall`, `system`, `information_schema`,
  `INFORMATION_SCHEMA`, or with the scratch prefix `cloudfall_` (`service_database_owner_reserved`)
- with two replicas or `clientAccess`, a server whose baseline declares no firewall
  (`service_clickhouse_unfirewalled`), or that has no IPv4 for the others to reach and admit
  (`service_clickhouse_server_unaddressed`)
- `clientAccess` with a source of every address (the S-006 rule)
- a server whose baseline declares no storage pool or several, or, for one replica, a pool
  without a secondary physical volume: the S-014 codes

It warns `CLICKHOUSE_SINGLE_REPLICA` for a production environment with one replica: one copy
of the data, and every restart stops the service.

### Audit and metrics

- `audit` reports drift when a server's installed version differs from `version`, when
  `clickhouse-server.service` or `clickhouse-keeper.service` is not enabled and running,
  when a managed file differs from the rendered one, when a listener is on an address the
  declaration does not name, and when a requirement of a server is unmet (S-011)
- It reports the state of the copy: Keeper without quorum or with a voter missing, a
  replicated table that is read-only, a replica more than five minutes behind, a
  registration in Keeper of a server that is not declared, and a table in a declared
  database that is not replicated
- With `metrics.enabled`, the collector on each server scrapes the metrics endpoint
  ClickHouse and Keeper serve on loopback, with the service id as the label the other
  exporters use. The services dashboard (S-016) gains ClickHouse panels: replication delay,
  read-only tables, Keeper quorum, memory against the limit, and disk

### Documentation

`docs/clickhouse-service-guide.md` covers declaring one replica, growing to two with the
third voter, what an application lists in its client, `insert_quorum`, backups and the
drill, a version change inside a series and to the next one, replacing a lost replica and a
lost third voter, what a team sees while Keeper has no quorum, and moving tables from a
database that is not replicated.

Touches `config/schemas/v1/service.schema.json`, `config/examples/services/`,
`sdk/src/cloudfall/domain.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/requirements.py`,
`sdk/src/cloudfall/secrets.py`, `sdk/src/cloudfall/audit.py`,
`engine/src/cloudfall_engine/ansible_inventory.py`, a new role
`engine/ansible/roles/cloudfall_clickhouse`, `engine/ansible/playbooks/services.yml`,
`engine/ansible/playbooks/backup.yml`,
`engine/ansible/roles/cloudfall_logging_collector/templates/config.alloy.j2`,
`engine/ansible/roles/cloudfall_logging_backend/files/dashboards/cloudfall-services.json`,
ROADMAP.md, ARCHITECTURE.md, and the guide.

## Acceptance criteria

- S-022-1: A project with three `Server` resources, their baselines, and the declaration
  above passes `cloudfall config validate` with exit 0, and `cloudfall-engine inventory
  render` carries the service with its two replicas, its three voters, its database and no
  password value
- S-022-2: For the project in `config/examples` without a ClickHouse service, the rendered
  inventory is byte-identical before and after the change
- S-022-3: After `services.yml` converges a service with one replica and no `clientAccess`
  on a fresh Debian 13 host, `SELECT version()` as `cloudfall` on `127.0.0.1:9000` reports
  the declared version, the packages are held, `max_server_memory_usage` is the declared
  one, the data directory is on the `clickhouse` volume, no listener of ClickHouse or Keeper
  exists on a non-loopback address, a login as `default` is refused, and the declared
  database has the `Replicated` engine
- S-022-4: A second converge changes nothing (`changed=0`) and restarts no server; a
  `maxServerMemoryMb` above nine tenths of the host's memory stops the converge before any
  task changes the host
- S-022-5: `events` connects on loopback with its password, can create a table in `events`
  with `ENGINE = MergeTree`, and the table's engine is the replicated form; a table with
  `ENGINE = Memory` is refused; `events` is refused on another database and can't create a
  user. With Keeper stopped, `cloudfall` still logs in
- S-022-6: The password of `events`, of `cloudfall` and the replication credentials appear
  in none of the rendered inventory, playbook output, command answers or receipts; the
  statement and the file that set a password hold a hash and never the password; a
  `passwordRef` whose file or key is missing stops the converge with `PRECONDITION` before
  any playbook runs
- S-022-7: Removing a declared database and converging leaves the database and its rows in
  place, the user can no longer log in, and `audit` lists both as undeclared; a user created
  by hand is left alone and listed as unmanaged
- S-022-8: Converging the declaration above on three hosts gives two replicas and three
  Keeper voters with a leader; a table created and filled on `h1` exists on `h2` with the
  same rows; a row inserted on `h2` appears on `h1`; `h3` holds no `clickhouse-server`
  package and no table data
- S-022-9: Each server's ruleset admits the replication port from the other replica only,
  the Keeper client port from the replicas only and the Raft port from the other voters
  only; each of those connections is encrypted and verified against the project CA, and a
  connection without the replication credentials is refused
- S-022-10: Adding a second replica and two voters to the service of S-022-3 and converging
  gives the pair of S-022-8 with every table and row the first replica held, no table
  recreated on the first replica, and a Keeper ensemble that kept its data and never had two
  leaders; a second converge changes nothing and fetches nothing again
- S-022-11: With one voter stopped, both replicas still take inserts. With two voters
  stopped, an insert is refused, a `SELECT` on each replica still answers, `audit` reports
  Keeper without quorum, and a converge stops with `PRECONDITION` without restarting a
  replica; when a voter returns, inserts work again with no step by a person
- S-022-12: With `h2` stopped, `h1` takes inserts and `audit` reports the replica as
  missing; when `h2` returns it receives those rows with no converge
- S-022-13: Changing `version` to a later patch of the same series on the pair restarts one
  server at a time in the declared order, an insert loop against the pair that retries on
  the other replica loses no row, and every server reports the new version
- S-022-14: Changing `version` to the next accepted series does the same as S-022-13; an
  older `version` and a series before the installed one are refused before any package
  changes
- S-022-15: With `h2` destroyed, replacing it by an empty `h4` in `replicas` and
  `keeper.servers` and converging gives a pair of `h1` and `h4` with every row, three
  voters, and no registration of `h2` left in Keeper; `h1` took inserts throughout
- S-022-16: A declared replica whose volume was recreated empty joins again by one converge
  with every row; a replica that holds a table and can't attach stops the converge with no
  table dropped
- S-022-17: `cloudfall backup run clickhouse-main` writes one backup and one count file and a
  `BackupReceipt`; `cloudfall backup verify clickhouse-main` restores each database into a
  scratch database, finds equal tables and row counts, drops the scratch database and writes
  its receipt; with no backup it fails and says so; a row inserted after the backup does not
  fail the drill; the scratch database never exists on the other replica, and no path of
  it is left in Keeper; a table created with a literal Keeper path fails the drill before
  any restore and is named
- S-022-18: Backups older than `retentionDays` are gone after the next backup run, and the
  newest is kept
- S-022-19: Each refusal listed under Validation fails `cloudfall config validate` with
  `CONFIG_INVALID` and its code; `version: 25.8.33.6` and `version: 26.10.4.207` each fail
  with `service_clickhouse_version`; a version of a series within 90 days of its end-of-support
  date validates with the warning `CLICKHOUSE_VERSION_ENDING` naming the date; one replica
  in a production environment validates with `CLICKHOUSE_SINGLE_REPLICA`
- S-022-20: With `clientAccess` of `allowedSources: [10.0.0.0/24]`, `events` connects to
  each replica from an allowed address on 9440 and on 8443 verifying the project CA, a plain
  connection to 9000 or 8123 from that address is refused, and each ruleset holds `ip saddr
  10.0.0.0/24 tcp dport { 8443, 9440 } accept` and no unrestricted rule for a ClickHouse port
- S-022-21: `cloudfall audit` reports drift when the installed version differs from the
  declaration, when a unit is stopped, when a managed file is changed by hand, when a table
  with a non-replicated engine exists in a declared database, and when a replica is more
  than five minutes behind
- S-022-22: With `metrics.enabled`, the rendered collector configuration of each replica and
  of the third voter holds one scrape of its ClickHouse or Keeper endpoint, labelled with
  the service id, and none without it; the services dashboard shows the replication delay
  of each replica and the Keeper quorum

## Out of scope

- Shards and `Distributed` tables: one shard, so a table is never larger than one server
- More than two replicas, and five Keeper voters
- Keeper on three hosts of its own, apart from the replicas
- Databases of another engine than `Replicated`, and `ON CLUSTER` statements
- Removing a replica without replacing it, and going from three voters to one
- A Keeper ensemble shared by services, or offered to anything but its ClickHouse service
- ZooKeeper as the coordination service
- Automatic replacement of a lost server: an operator changes the declaration
- A proxy or load balancer in front of the pair (chproxy, HAProxy); the client lists both
  replicas
- Backups to S3-compatible storage and incremental backups
- Point-in-time recovery
- Moving in from a ClickHouse server Cloudfall does not manage, and from ClickHouse Cloud
- Storage tiers, S3-backed tables and object storage disks
- Read-only users, row policies, quotas and settings profiles in the declaration
- A pass-through of server settings in the declaration
- Skipping a series, and moving to an older version
- Monthly stable releases
- Tables, materialized views, dictionaries and their schemas: the application's own
- Connections between components and stores with a generated user per connection:
  cloudfall-dev/cloudfall#216
- Dropping a database or a user

## Decisions relied on

- D-9: an owner password reaches the server only as a hash computed on the controller;
  stated for PostgreSQL, applied here to ClickHouse owners
- D-18: a mirrored volume meets an expendable requirement

## Issues

## Verification

No criterion is verified yet.
