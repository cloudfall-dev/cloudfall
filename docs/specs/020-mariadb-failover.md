# S-020: Automatic failover for a MariaDB pair

status: draft
serves: G-2, G-3

## Problem

S-019 gives a MariaDB primary and a replica, and a promotion that happens only when a person
changes the declaration and converges. When the primary's host dies at night, the database is
down until someone does that (cloudfall-dev/cloudfall#416).

Two things are missing:

- nothing watches the pair and decides that the primary is gone
- nothing stops a primary that is alive but cut off from taking writes while its replica is
  promoted, so two servers can end up with different data

The first is solved software: replication-manager (Signal18, GPLv3) watches a replication
topology and promotes a replica, and has done so in production for years. Cloudfall runs it
instead of writing a controller of its own.

The second it leaves to a proxy, which cuts the clients off. Cloudfall has no proxy in the
path (S-021: the application asks the servers), so an application on the cut-off primary's
own host keeps writing through the socket. The bench run of 2026-11-16
(`docs/proving-runs/2026-11-16-replication-manager-bench.md`) measured it: with the primary
cut off and nothing else added, 51 rows were written to it that exist on no other server,
while the manager promoted the replica. So the server has to stop by itself, and that part
is Cloudfall's: a watchdog on each database host, and timings that make the manager wait for
it.

## Behaviour

### Declaration

Failover is declared on the replica, the server that would be promoted:

```yaml
mariadb:
  version: 11.8.9
  innodbBufferPoolMb: 4096
  replication:
    role: replica
    serverId: 2
    primary: mariadb-main
    failover:
      manager: mgmt1
      listenAddress: 10.0.0.3
      fence: watchdog
      fenceSeconds: 10
```

| Field | Required | Meaning |
|---|---|---|
| `manager` | yes | The `Server` that runs replication-manager for this pair: the management host, never the server of either database. Pairs of one environment that name the same server share one manager |
| `listenAddress` | yes | The address of the replica's server that the manager and the primary's watchdog connect to; one address, never a wildcard. The replica listens on it as a primary listens on `replication.listenAddress` |
| `fence` | no | `watchdog`, the default: each server turns its own writes off when it is cut off (below). `none`: no watchdog is installed (see Without the watchdog) |
| `fenceSeconds` | no | With `fence: watchdog`, how long a primary keeps taking writes with no witness, 5 to 60, 10 when absent |

The block is the consent: a pair without it is never promoted by anything but a declaration
change (S-019), and removing the block and converging takes the pair back to that. The
manager is the formation's own layer, below the operator, as ROADMAP.md M14 puts it for
PostgreSQL: an `OperatorPolicy` still can't grant a promotion, and the operator still never
starts one.

### The manager

On the `manager` server, a new role `cloudfall_replication_manager` installs
`replication-manager-osc`, the open-source build, from Signal18's APT repository, which
serves Debian 12 and 13 on amd64 and arm64 and keeps earlier versions. It installs the one
version this Cloudfall release was proven with, held like every pinned package (S-011), and
renders one cluster section per pair. Nothing in it is set by hand:

| Setting | Value | Why |
|---|---|---|
| The two servers and the preferred primary | From the pair's declarations | |
| `failover-mode` | `automatic` | The manager's default is manual |
| `autorejoin` | off | A former primary comes back through the declaration (S-019), not by the manager's own rejoin |
| Proxies | None | S-021 routes; the manager only decides |
| `force-slave-heartbeat` | on, with its three-second period | The manager's check that the replica lost the primary reads this heartbeat |
| `failover-falsepositive-ping-counter` | Enough two-second pings to cover `fenceSeconds` plus ten seconds | The wait when the primary stops answering |
| `failover-falsepositive-heartbeat-timeout` | `fenceSeconds` plus five seconds | The wait when the replica's link is lost; with the heartbeat period and the manager's ticker it is `fenceSeconds` plus ten |
| `api-bind` | `127.0.0.1` | The manager's default is every address |
| `api-credentials` | A generated login | The default login is public and the package accepts it |
| External arbitration, Cloud18 registration | Off | One manager is its own arbitrator; nothing is sent to a hosted service |

The manager's API and web interface so listen on loopback of the manager server only, and
the generated login is sops-encrypted in the project. The package ships its configuration
readable by everyone; the role makes it `0600`, owned by the manager's user.

With `failover` declared, converge also creates two accounts on the primary, which reach the
replica by replication: `cloudfall_failover` for the manager, and `cloudfall_watch` for the
watchdogs (status only). The manager's account gets the grants its documentation lists for
MariaDB 10.5 and later, plus `SLAVE MONITOR`, and no others: without that one the manager
can't read a replica's status on MariaDB 11.8 and promotes nothing, which the bench showed. Their generated passwords are sops-encrypted in the project and the
servers hold hashes (D-9). The manager needs the plaintext of its own account and of the
replication account to log in; they land in its configuration, a `0600` file owned by its
user on the manager server, and nowhere else. Both servers admit `bind.port` from the
manager server and from each other, and from nowhere else for these accounts.

### A server's writes

A server of a pair with `failover` always starts read-only; the manager turns writes on for
the server it holds as primary, which the bench saw it do four seconds after a restart. So a
former primary that returns after a failover never takes a write by itself.

With `fence: watchdog`, a systemd unit the role installs on each database server only ever
turns writes off. It looks every second for a witness:

- **the manager:** one of its sessions on this server ran a statement within the last
  `fenceSeconds`. The watchdog reads that from the server's own process list, so it needs no
  way to the manager: the manager polls every two seconds, and on the bench the idle time
  of its sessions never passed two seconds while it reached the server and grew without
  limit once it was cut off, or
- **the replica:** asked within the last `fenceSeconds`, it answered that it is read-only and
  its link to this server is running

When a writable server has had neither for `fenceSeconds`, the watchdog turns `read_only` on
and closes the client connections. It does not turn writes back on: the manager does, when
it reaches the server again and still holds it as primary.

- a manager that is down stops nothing while the primary still reaches its replica
- a primary cut off from both stops taking writes after `fenceSeconds`, on its own, with
  nobody able to reach it
- a server that has had no replica since its writes were last turned on is never fenced:
  after a promotion the new primary stands alone, and nobody is left to be promoted beside
  it

The rule that holds it together, and what the partition criteria test: on either path, an
unreachable primary or a lost link, the manager waits at least ten seconds longer than
`fenceSeconds` before it promotes. Cloudfall renders both sides from the one declared
number, so they can't drift apart. On the bench, with the link path left at the manager's
default, the margin was two seconds; that is why the heartbeat timeout is rendered too.

What the watchdog does not do: it bounds the loss, it does not remove it. Rows written to a
cut-off primary before it stops are on no other server (eight or nine on the bench, at one
row a second and eight seconds). When that server returns, the converge stops and reports
the divergence (S-019).

### Without the watchdog

A pair may decline it with `fence: none`:

- no watchdog unit is installed, and nothing on a server turns its writes off by itself
- the servers still start read-only and the manager still turns writes on, so a returning
  former primary stays read-only
- the manager's two waits are left at its defaults, so it promotes sooner: nine seconds on
  the bench
- what is given up: a primary that is alive but cut off keeps taking writes from any client
  that still reaches it, first of all an application on its own host that connects through
  the socket, while the replica is promoted

`config validate` accepts it with the warning `MARIADB_FAILOVER_UNFENCED`, which says the
last point. It suits data that can be rebuilt; it does not suit a database with an
application on the same host.

### After a failover

The manager's post-failover script calls `cloudfall failover record` on the manager server,
which writes one `FailoverReceipt` into `record/failover/` (D-16), schema-validated, with no
secret in it:

- the pair, and which server was the primary and which the replica
- when the manager declared the primary failed, and when the replica's writes were turned on
- the last GTID the replica applied, and the last GTID the manager had seen on the primary:
  the difference is what may be lost
- the manager's version, and its own crash record for the event

`cloudfall why` answers for a failover from this receipt alone, and `cloudfall failover
status` prints each pair's state as the manager's API gives it.

The declaration still names the old primary. Converge would turn it back into one, so it
refuses: while the newest receipt for a pair names a primary the declaration does not,
`services.yml` stops for both services with `PRECONDITION` and
`service_mariadb_failover_undeclared`, before any playbook runs, and `audit` reports the
same. The person or the agent then swaps the roles in the declaration; the next converge
attaches the old primary as the replica when its history allows, or stops and reports the
divergence (S-019), and the failover block now sits on the new replica.

With `failover` declared, a replica retries its link every five seconds instead of MariaDB's
sixty: on the bench a replica stayed a minute behind after its primary restarted, and a
replica that is behind is one the manager will not promote.

### What is not a trigger

- **Monitoring.** A filling disk or a degraded array arrive through alerts and lead to a
  planned switch (S-019) before the server fails; monitoring is never a witness
- **A stalled write path.** A primary that answers but does not commit is not promoted over
  in this spec
- **An application's report**

### Validation

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- `failover` on a primary, on a standalone server, or on a replica with `source`
  (`service_mariadb_failover_placement`)
- a `manager` that is not a declared `Server` of the same environment, or that is the server
  of either database of the pair (`service_mariadb_failover_manager`)
- a `listenAddress` that is a wildcard or loopback (`service_mariadb_failover_access`)
- `fenceSeconds` outside 5 to 60, or `fenceSeconds` next to `fence: none`
  (`service_mariadb_failover_fence`)
- a primary with two replicas that both declare `failover` (`service_mariadb_failover_ambiguous`)
- a database or `owner` named `cloudfall_failover` or `cloudfall_watch`
  (`service_database_owner_reserved`)

### Documentation

`docs/mariadb-failover-guide.md` covers declaring failover on a pair, what the manager is and
where its interface listens, reading `failover status` and a receipt, what to do after a
failover, declining the watchdog, and the limits: asynchronous replication loses the
primary's last transactions, a cut-off primary loses up to `fenceSeconds` of writes, and the
receipt says which. It names replication-manager's licence (GPLv3) and that Cloudfall
installs it unmodified.

Touches `config/schemas/v1/service.schema.json`, a new
`config/schemas/v1/failover-receipt.schema.json`, `sdk/src/cloudfall/domain.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/requirements.py`, `sdk/src/cloudfall/secrets.py`,
`sdk/src/cloudfall/audit.py`, `sdk/src/cloudfall/why.py`, `sdk/src/cloudfall/app.py`, a new
`sdk/src/cloudfall/failover.py`, `engine/src/cloudfall_engine/ansible_inventory.py`,
`engine/ansible/roles/cloudfall_mariadb`, a new role
`engine/ansible/roles/cloudfall_replication_manager`, `engine/ansible/playbooks/services.yml`,
`docs/operator-guide.md`, ARCHITECTURE.md, ROADMAP.md, and the guide.

## Acceptance criteria

- S-020-1: A project with the pair of S-019-12, a third `Server` and the `failover` block
  above passes `cloudfall config validate` with exit 0; for a project without the block, the
  rendered inventory is byte-identical before and after the change
- S-020-2: Each refusal listed under Validation fails `cloudfall config validate` with
  `CONFIG_INVALID` and its code
- S-020-3: After a converge, the manager server runs replication-manager at the pinned
  version with the package held, its rendered configuration holds the settings of the table
  above for the declared `fenceSeconds`, its API answers on loopback only and refuses the
  default login, and no password appears in the rendered inventory, playbook output, command
  answers or receipts
- S-020-4: After a converge, both database servers hold `cloudfall_failover` and
  `cloudfall_watch`, the watchdog unit is enabled and running on both, each server's ruleset
  admits `bind.port` from the manager server and the other server only, the manager shows
  the pair as one primary and one replica, and a second converge changes nothing
- S-020-5: Restarting the primary leaves it read-only at start and writable again within
  ten seconds with the manager running, with no person and no converge
- S-020-6: With the primary cut off from both the manager and the replica by firewall
  rules, while a client on its own host writes one row a second through the socket, the
  primary is read-only and has closed its client connections within `fenceSeconds` plus two
  seconds, and the replica takes its first write at least five seconds after that
- S-020-7: With the primary's host powered off, the replica is writable within
  `fenceSeconds` plus twenty seconds, and a `FailoverReceipt` names both servers, the two
  times and the two GTID positions
- S-020-8: With the manager cut off from the primary and the replica still following it,
  nothing is promoted, the primary stays writable, and `failover status` shows the manager's
  reason
- S-020-9: In the state of S-020-8, the replica's link is stopped by hand: the old primary
  is read-only at least five seconds before the replica takes its first write
- S-020-10: With the manager stopped and the primary reaching its replica, the primary
  stays writable for ten minutes
- S-020-11: In S-020-6, S-020-7 and S-020-9, a client under the S-021 contract never sees
  two writable servers, and no second has a row accepted by both
- S-020-12: After S-020-7, the new primary restarted with the manager stopped and no replica
  attached is not fenced once the manager has opened its writes; the old primary powered on
  is read-only and stays so, and the manager does not reattach it
- S-020-13: After S-020-7, `services.yml` for either service stops with `PRECONDITION` and
  `service_mariadb_failover_undeclared` before any playbook runs, and `audit` reports the
  stale declaration; after the roles are swapped in the declaration, a converge attaches the
  old primary as the replica with no seed, and the next failover promotes it
- S-020-14: After S-020-6, when the cut ends, the old primary is not attached and the
  converge reports the divergence, with no row dropped on either server
- S-020-15: `cloudfall why` for the receipt of S-020-7 names the times and the GTIDs,
  reading nothing but the record
- S-020-16: A replica whose link has been down for longer than the manager's limit is not
  promoted when the primary fails, and `failover status` says why
- S-020-17: With `failover` declared, a replica whose primary restarts is caught up again
  within fifteen seconds of the primary answering
- S-020-18: With `fence: none`, `config validate` passes with the warning
  `MARIADB_FAILOVER_UNFENCED`, no watchdog unit exists on either server after a converge,
  the manager's two waits are at its defaults, and the powered-off primary of S-020-7 is
  replaced within fifteen seconds
- S-020-19: With `fence: none` and the primary cut off as in S-020-6, the client on its own
  host keeps writing through the socket after the replica is promoted; when the cut ends,
  the old primary turns read-only, is not attached, and the converge reports the divergence
- S-020-20: Removing the `failover` block and converging removes the watchdog unit, the
  pair's section from the manager and both accounts, leaves the primary writable across a
  restart, and removes replication-manager from the manager server when no pair is left
- S-020-21: Restarting the manager during the wait before a promotion ends with one
  promotion or none, never two writable servers, and with one receipt when it promotes
- S-020-22: A proving run recorded in `docs/proving-runs/` on three hosts (the manager
  server and a pair) shows S-020-6, S-020-7 and S-020-9 with their receipts and the measured
  times

## Out of scope

- A controller of Cloudfall's own
- A proxy on application hosts that follows `read_only`: the answer for an application the
  team can't change, and a spec of its own
- Semi-synchronous replication; until then a failover may lose the primary's last
  transactions, and the receipt names the two positions
- replication-manager's own rejoin, backups, provisioning, proxy management and alerting:
  they stay off, and Cloudfall's own cover them
- External arbitration and more than one manager
- Promotion over a stalled write path, and any other trigger than a primary that stops
  answering
- A planned switch started by an alert: it stays a declaration change (S-019)
- Editing the declaration after a failover
- More than one replica that may be promoted
- PostgreSQL: ROADMAP.md M14
- The application's contract and wrappers: S-021

## Decisions relied on

- D-9: a password reaches a server only as a hash computed on the controller
- D-16: receipts live in the committed `record/` directory

## Issues

## Verification

No criterion is verified yet. The bench run of 2026-11-16 exercised the manager and a
hand-written watchdog in containers and is the ground for the design, not a proof of any
criterion. Its second part installed the package on Debian 12 and 13, ran the manager with
the grants above, and watched its sessions from the server.
