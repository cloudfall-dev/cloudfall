# S-020: Automatic failover for a MariaDB pair

status: approved
serves: G-2, G-3

## Problem

S-019 gives a MariaDB primary and a replica, and a promotion that happens only when a person
changes the declaration and converges. When the primary's host dies at night, the database is
down until someone does that, and every application still points at the dead host
(cloudfall-dev/cloudfall#416).

Three things are missing:

- nothing watches the pair and decides that the primary is gone
- nothing stops a primary that is alive but cut off from taking writes while its replica is
  promoted, so two servers can end up with different data
- an application has no way to find the new primary: the address sits in its environment,
  written by hand

A proxy on every application host would answer the third, and is the usual answer. It also
takes away the fastest way in: an application on the primary's own host connects through the
unix socket today. This spec keeps that path and puts the fallback in the application.

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
      listenAddress: 10.0.0.3
      controllerSources:
        - 10.0.0.1/32
      fenceSeconds: 15
```

| Field | Required | Meaning |
|---|---|---|
| `listenAddress` | yes | The address of the replica's server that the controller and the primary's watchdog connect to; one address, never a wildcard. The replica listens on it as a primary listens on `replication.listenAddress` |
| `controllerSources` | yes | The CIDRs the controller connects from: the management host. Both servers admit `bind.port` from them |
| `fenceSeconds` | no | How long a primary keeps taking writes with no witness (below), 10 to 120, 15 when absent |

The block is the consent: a pair without it is never promoted by anything but a declaration
change (S-019), and removing the block and converging takes the pair back to that. The
controller is the formation's own layer, below the operator, as ROADMAP.md M14 puts it for
PostgreSQL: an `OperatorPolicy` still can't grant a promotion, and the operator still never
starts one.

### The parts

| Part | Where | What it does |
|---|---|---|
| The controller | The management host, `cloudfall failover run` | Watches every pair of the project that declares `failover`, and is the only thing that promotes |
| The watchdog | Each server of such a pair, a systemd unit the role installs | Turns the server's writes on and off by what it can see; never promotes |
| The application's fallback | The application | Finds the writable server again after a switch |

With `failover` declared, converge also creates two accounts on the primary, which reach the
replica by replication: `cloudfall_failover` for the controller (it may read status, switch
`read_only`, stop and forget replication and close client connections, and nothing else)
and `cloudfall_watch` for the watchdogs (status only). Their generated passwords are
sops-encrypted in the project; the servers hold hashes (D-9). Both connect over TLS and
verify the server against the project CA. The primary admits `bind.port` from
`controllerSources` and from the replica's server; the replica admits it from
`controllerSources` and from the primary's server.

### A server's writes

A server of a pair with `failover` always starts read-only. Its watchdog turns writes on,
and keeps them on, only while the server is the pair's primary and has a witness:

- **the controller's lease:** the controller renewed it within the last `fenceSeconds`, or
- **its replica:** the watchdog asked the replica within the last `fenceSeconds`, and the
  replica answered that it is read-only and still follows this server

When neither holds, the watchdog turns `read_only` on and closes the client connections.
When one holds again, it turns writes back on. So:

- a primary that restarts takes writes again by itself as soon as it has a witness
- a controller that is down stops nothing while the primary still reaches its replica
- a primary cut off from both stops taking writes after `fenceSeconds`, on its own, with
  nobody able to reach it

After a promotion the new primary has no replica. The controller marks it the pair's sole
server, and a sole server keeps its writes on without a witness: nobody is left to be
promoted beside it. The mark survives a restart and is cleared when a replica is attached
again (S-019).

### The controller

`cloudfall failover run` is a long-running command on the management host, started under
systemd like `cloudfall operator run`. It reads the project, takes the controller lock (a
second controller for the same project refuses to start), and keeps one connection open to
each server of each pair.

Every second it pings each server over that connection with a two-second timeout: whether
it is read-only, its GTID position and, on the replica, the state of its link to the
primary. A ping the primary answers renews its lease. The heavier logic runs only after a
ping is missed. It writes one JSON line per change of a pair's state, like the operator's
passes, and `cloudfall failover status` prints each pair's state from the servers and the
record.

A primary is **suspect** after three pings in a row with no answer. The controller then
asks the replica what it sees:

| Controller reaches the primary | Replica's link to the primary | Action |
|---|---|---|
| no | up | The controller's own network; nothing, and a finding in `status` |
| yes | down | Replication is broken, not the primary; nothing, and `audit` reports it (S-019) |
| no | down | Promote, as below |
| replica unreachable | | Nothing to promote |

Promotion is a fixed sequence, and each step is safe to repeat after a controller restart:

1. Stop renewing the primary's lease. From here the controller sends the primary only
   reads that renew nothing
2. Stop replication on the replica. From this moment the replica answers the primary's
   watchdog that it no longer follows it, so the primary has lost both witnesses, whichever
   way the network is cut
3. Wait `fenceSeconds` plus five seconds, counted from the later of the last lease renewal
   sent and step 2. If the primary answers during the wait and is still writable, the
   promotion is called off: replication starts again, the lease is renewed, and the receipt
   says `aborted`
4. Let the replica apply what it had already received, read its GTID position, forget its
   replication settings, mark it sole, and turn its writes on
5. Write the receipt

The controller never promotes a replica whose link was already down before the primary
became suspect: what it holds may be hours old. That pair stays as it is and `status` says
why.

What the wait guarantees, and what every criterion below tests: at no moment do both
servers of a pair take writes.

### After a failover

The declaration still names the old primary. Converge would turn it back into one, so it
refuses: while the record's newest failover receipt for a pair names a primary the
declaration does not, `services.yml` stops for both services with `PRECONDITION` and
`service_mariadb_failover_undeclared`, before any playbook runs, and `audit` reports the
same. The person or the agent then swaps the roles in the declaration; the next converge
attaches the old primary as the replica when its history allows, or stops and reports the
divergence (S-019), and the failover block now sits on the new replica.

The old primary, when it comes back by itself, starts read-only and stays so: the
controller renews no lease for a server the record no longer names as primary, and the new
primary does not follow it.

### The record

Each promotion, finished or called off, is one `FailoverReceipt` in `record/failover/`
(D-16), schema-validated, with no secret in it:

- the pair, and which server was the primary and which the replica
- when the controller last reached the primary, and when the replica's link went down
- each step of the sequence with its time
- the last GTID the replica applied, and the last GTID the controller had seen on the
  primary: the difference is what may be lost
- the outcome: `promoted` or `aborted`

`cloudfall why` answers for a failover from this receipt alone.

### What the application does

No proxy is in the path, so the application carries the rule. Its environment holds the
fixed list of both servers of the pair, and for a server on its own host the socket path.
The contract, stated in the guide:

1. Connect to the server last known to be writable, through the socket when it is local
2. On a connection error, or on a statement refused because the server is read-only, ask
   each server of the list whether it is read-only, take the one that is not, reconnect and
   run the transaction again
3. When no server is writable, fail the request and try again on the next one: a failover
   is over within a minute
4. Never write to a server that says it is read-only, and never cache the choice longer than
   the connection lives

An application that fails over uses an `owner` with a password (S-019): the passwordless
socket form works on the database's own host only. It still connects through the socket
there, with the password.

`examples/failover-clients/` holds a reference wrapper for Python and one for PHP: one file
each, with no dependency beyond the application's own driver, that implement the four
rules around a connection factory. They are examples to copy, tested in CI against a pair
the test starts.

### What is not a trigger

- **Monitoring.** A filling disk, a degraded array or rising errors arrive through alerts
  and lead to a planned switch (S-019) before the server fails. Monitoring is never a
  witness: it is slower than the decision needs, and the decision must not depend on the
  monitoring stack being up
- **A stalled write path.** The controller reads the primary's write counters in the same
  ping and takes no test write: no service table, nothing in the binary log. A primary that
  answers but whose commits have stopped while writes wait is shown in `status` and reported
  by `audit`; it is not promoted over in this spec, because a heuristic must not start a
  step that can lose transactions
- **An application's report.** An application on the primary's host is the last to notice a
  network cut, and its errors often have other causes

### Validation

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- `failover` on a primary, on a standalone server, or on a replica with `source`
  (`service_mariadb_failover_placement`)
- a `listenAddress` that is a wildcard or loopback, or `controllerSources` that is empty or
  holds `0.0.0.0/0` or `::/0` (`service_mariadb_failover_access`)
- `fenceSeconds` outside 10 to 120 (`service_mariadb_failover_fence`)
- a primary with two replicas that both declare `failover` (`service_mariadb_failover_ambiguous`)
- a database or `owner` named `cloudfall_failover` or `cloudfall_watch`
  (`service_database_owner_reserved`)

### Kind-neutral parts

ROADMAP.md M14 plans the same for PostgreSQL. The controller's loop, the lease and the fence
rule, the decision table, the promotion sequence with its wait, the receipt and the refusal
of a stale declaration are written against an interface a kind implements: ping, renew,
stop following, promote, mark sole. Only that implementation, the accounts, the watchdog's
statements and the application contract are MariaDB's.

### Documentation

`docs/mariadb-failover-guide.md` covers declaring failover on a pair, running the controller
under systemd, reading `failover status` and a receipt, what to do after a failover, the
application contract with the two wrappers, and the limits: asynchronous replication loses
the primary's last transactions, and the receipt says which.

Touches `config/schemas/v1/service.schema.json`, a new
`config/schemas/v1/failover-receipt.schema.json`, `sdk/src/cloudfall/domain.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/requirements.py`, `sdk/src/cloudfall/secrets.py`,
`sdk/src/cloudfall/audit.py`, `sdk/src/cloudfall/why.py`, `sdk/src/cloudfall/app.py`, a new
`sdk/src/cloudfall/failover.py`, `engine/src/cloudfall_engine/ansible_inventory.py`,
`engine/ansible/roles/cloudfall_mariadb`, `engine/ansible/playbooks/services.yml`,
`examples/failover-clients/`, `docs/operator-guide.md`, ARCHITECTURE.md, ROADMAP.md, and
the guide.

## Acceptance criteria

- S-020-1: A project with the pair of S-019-12 and the `failover` block above passes
  `cloudfall config validate` with exit 0; for a project without the block, the rendered
  inventory is byte-identical before and after the change
- S-020-2: Each refusal listed under Validation fails `cloudfall config validate` with
  `CONFIG_INVALID` and its code
- S-020-3: After a converge of the pair, both servers hold `cloudfall_failover` and
  `cloudfall_watch`, the watchdog unit is enabled and running on both, each server's ruleset
  admits `bind.port` from `controllerSources` and the other server only, and neither
  password appears in the rendered inventory, playbook output, command answers or receipts
- S-020-4: With the controller stopped, restarting the primary leaves it read-only at start
  and writable again within ten seconds, with no person and no converge
- S-020-5: With the primary cut off from both the controller and the replica by firewall
  rules, it is read-only and has closed its client connections within `fenceSeconds` plus
  two seconds, while its own host still reaches it through the socket
- S-020-6: With the controller stopped and the primary reaching its replica, the primary
  stays writable for ten minutes
- S-020-7: With the primary's host powered off, the replica is writable within
  `fenceSeconds` plus fifteen seconds of the last answered ping, replication is forgotten on
  it, it is marked sole, and a `FailoverReceipt` with outcome `promoted` names both servers,
  each step's time and the last GTID applied
- S-020-8: With the controller cut off from the primary and the replica still following it,
  nothing is promoted and `failover status` reports the controller's own network
- S-020-9: With the replica's link stopped by hand and the primary reachable, nothing is
  promoted
- S-020-10: With the primary cut off from the controller only, while it still reaches the
  replica, and the replica's link then stopped by hand, the replica is promoted and the old
  primary is read-only before the replica takes its first write
- S-020-11: In S-020-5, S-020-7 and S-020-10, a client that writes one row a second to
  whichever server accepts it never has two servers accept a row within the same second,
  and every row the new primary holds after the switch carries a GTID of one history
- S-020-12: When the primary answers again during the wait of step 3, the promotion is
  called off: the replica follows the primary again, the primary stays writable, and the
  receipt says `aborted`
- S-020-13: A replica whose link was down before the primary became suspect is not promoted,
  and `failover status` says why
- S-020-14: Killing the controller after step 2 and starting it again finishes the same
  promotion once, with one receipt; a second controller started for the same project exits
  with `PRECONDITION` while the first runs
- S-020-15: After S-020-7, the new primary restarted with the controller stopped is writable
  again by itself; the old primary powered on is read-only and stays so
- S-020-16: After S-020-7, `services.yml` for either service stops with `PRECONDITION` and
  `service_mariadb_failover_undeclared` before any playbook runs, and `audit` reports the
  stale declaration; after the roles are swapped in the declaration, a converge attaches the
  old primary as the replica with no seed, and the next failover promotes it
- S-020-17: `cloudfall why` for the receipt of S-020-7 names the witnesses, the times and
  the GTIDs, reading nothing but the record
- S-020-18: The Python and the PHP wrapper, each run against the pair through S-020-7 while
  writing one row a second, lose no row they were told was committed, report an error for
  the rows in flight at the switch, and write to the new primary within five seconds of its
  promotion with no restart of the client
- S-020-19: The wrappers connect through the unix socket to a writable server on their own
  host and over TCP with TLS to one elsewhere, and never send a write to a server that
  reports read-only
- S-020-20: A primary that answers pings while its commits have stopped and writes wait is
  shown in `failover status` and reported by `audit`, and is not promoted over
- S-020-21: Removing the `failover` block and converging removes the watchdog unit and both
  accounts' firewall rules, leaves the primary writable across a restart, and the controller
  no longer watches the pair
- S-020-22: A proving run recorded in `docs/proving-runs/` on three hosts (the management
  host and a pair) shows S-020-5, S-020-7 and S-020-10 with their receipts and the measured
  times

## Out of scope

- A proxy on application hosts that follows `read_only`: the answer for an application the
  team can't change, and a spec of its own
- Semi-synchronous replication; until then a failover may lose the primary's last
  transactions, and the receipt names the two positions
- Promotion over a stalled write path, and any other trigger than a primary that stops
  answering
- A planned switch started by an alert: it stays a declaration change (S-019)
- Editing the declaration after a failover: the controller writes the record, a person or
  an agent changes the config
- More than one replica that may be promoted, and choosing among several
- Failing back by itself when the old primary returns
- A controller on more than one host, and a failover of the controller
- PostgreSQL: ROADMAP.md M14, on the kind-neutral parts named above
- Rendering the list of servers into a component's environment: cloudfall-dev/cloudfall#216
- Published client packages; the wrappers are examples in the repository

## Decisions relied on

- D-9: a password reaches a server only as a hash computed on the controller
- D-16: receipts live in the committed `record/` directory

## Issues

## Verification

No criterion is verified yet.
