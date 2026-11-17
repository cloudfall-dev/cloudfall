# S-023: RabbitMQ service

status: draft
serves: G-1, G-3

## Problem

A `Service` is PostgreSQL, Redis, Elasticsearch or MariaDB (`serviceKind` in
`config/schemas/v1/service.schema.json`, `ServiceKind` in `sdk/src/cloudfall/domain.py`). A
team whose application passes its work through RabbitMQ runs that part by hand: the package
and its pin, the Erlang it needs, the data volume, the memory watermark, the cluster and its
shared cookie, TLS between the nodes, one user per virtual host, the copy of each queue on
the other nodes, and a backup of everything a broker holds besides messages. None of it is
declared, so `config validate`, `audit`, `backup run` and `backup verify` know nothing about
it, and an agent has no operation to propose for it. ROADMAP.md and ARCHITECTURE.md name
RabbitMQ under catalog breadth; this spec adds it.

RabbitMQ differs from the kinds before it in three ways:

- The nodes of a cluster are equals. A client connects to any of them, so there is no primary
  to declare, no promotion and no failover (S-019, S-020). What can be lost instead is the
  majority: with fewer than half of the nodes left, the cluster accepts no change and a
  replicated queue stops
- Two nodes are worse than one. A majority of two is two, so losing either node stops
  everything; RabbitMQ recommends against two-node clusters. The kind has one node or three
- Messages have no online backup. RabbitMQ can export its definitions (virtual hosts, users,
  permissions, queues, exchanges, bindings, policies) from a running node, and messages only
  by copying the data directory of a stopped one. What protects a message here is its copy on
  the other nodes, and the backup holds definitions only

## Behaviour

### Declaration

```yaml
apiVersion: cloudfall/v1
kind: Service
metadata:
  id: rabbitmq-main
  description: Job queue of the product
spec:
  serviceKind: rabbitmq
  environment: production
  server: h1
  bind:
    address: 127.0.0.1
    port: 5672
  rabbitmq:
    version: 4.4.1
    memoryHighWatermarkMb: 2048
    nodes: [h1, h2, h3]
    vhosts:
      - name: jobs
        owner: jobs
        passwordRef: { environment: production, path: /services/rabbitmq-main, key: JOBS_PASSWORD }
    clientAccess:
      allowedSources: [10.0.0.0/24]
  backup:
    directory: /var/backups/cloudfall/rabbitmq-main
    onCalendar: "*-*-* 02:50:00 UTC"
    restoreCheckOnCalendar: "Sun *-*-* 05:40:00 UTC"
    retentionDays: 14
  metrics:
    enabled: true
```

`spec.rabbitmq` is required for the kind and refused for the others:

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | Exact `X.Y.Z` of a supported series (below); `rabbitmq-server` is installed at `X.Y.Z-1` from Team RabbitMQ's APT repository on every node and held (`apt-mark hold`), so an unattended upgrade never restarts a node |
| `memoryHighWatermarkMb` | yes | `vm_memory_high_watermark.absolute` of each node, 512 or more: above it the node stops taking messages from publishers until it is below again |
| `nodes` | no | The servers of the cluster, one or three; must contain `spec.server`. Absent means `[spec.server]` |
| `vhosts` | no | The virtual hosts and their owners: `name`, `owner`, `passwordRef` (the S-006 owner form) |
| `clientAccess` | no | `allowedSources`: the addresses that may connect to every node from outside its server (below) |

`remoteAccess` (S-006) is refused for this kind: it names one listen address, and here every
node listens on its own.

### Supported versions

Cloudfall installs only the series that Team RabbitMQ still supports for the open-source
edition: `4.3` and `4.4` at the time of writing. RabbitMQ publishes a series about every six
months and supports the open-source edition of each for about nine, so two are accepted at
most times. Long-term support exists only under a commercial licence and is not what
Cloudfall installs. The accepted series live in one table in the SDK with the end of
community support RabbitMQ publishes for each (`4.3`: 2027-01-31, `4.4`: 2027-07-31), checked
against RabbitMQ's release information page when a Cloudfall release is cut. The table
follows the rule S-012 set for Elasticsearch:

- any other version fails validation; there is no flag that allows it
- within 90 days of a series' date, `config validate` warns `RABBITMQ_VERSION_ENDING` and
  names the date
- after the date, the warning stays, and `audit` reports the service as running an
  unsupported version
- the series leaves the table in the next minor release of Cloudfall, with a changelog entry

Erlang comes from the Debian release, not from a second repository: Debian 13 ships Erlang
27, both accepted series need 27.0 or later, and Team RabbitMQ documents no Erlang repository
of its own for Debian 13. The role installs the Erlang packages RabbitMQ's guide lists, holds
them, and refuses a host whose Erlang is outside the range the declared `version` supports.
The supported range of each series is in the same SDK table.

Because a series lasts nine months, moving to the next one is part of the kind:

- a change of `version` inside a series, or to the next accepted series, is a rolling change:
  one node at a time, `spec.server` last
- before the first node stops, the role enables every stable feature flag, which RabbitMQ
  requires of a cluster that is about to run two versions; after the last node it enables
  the flags the new version brings
- before a node stops, the cluster must have no alarm, and stopping that node must leave
  every quorum queue with its majority (`rabbitmq-diagnostics
  check_if_node_is_quorum_critical`). After it starts, the role waits until the node is
  healthy (below) before it moves on. With one node the change is a short stop of the service
- an older `version` than the installed one, and a series that is not the installed one or
  the next accepted one, are refused by the role before anything changes. RabbitMQ does not
  support downgrades

### One node is a cluster that waits for two more

The role configures a service with one node exactly as one with three, so growing is adding
two servers to `nodes` and converging. No queue is redeclared and no message is moved by
hand.

- The node name of a server is `rabbit@` and its hostname. RabbitMQ can't rename a node that
  holds a quorum queue, so `audit` reports a node whose hostname no longer matches its data
  directory, and the role refuses to start it
- A node finds the others by a list of the declared node names in its configuration. The role
  makes every node's hostname resolve on the other nodes
- Every declared virtual host has `quorum` as its default queue type, so a durable queue an
  application declares with no type is a quorum queue: one copy on one node, three on three.
  An exclusive or non-durable queue stays a classic queue on the node that took the
  declaration and dies with its connection or with that node, which is what those words ask
  for
- A durable classic queue is the one thing that looks kept and is not copied. Nothing
  refuses it, because an application may want one; `audit` lists each
- The target size of a quorum queue is three, and continuous membership reconciliation is
  on. A queue declared on one node has one member; once the other two nodes have joined,
  RabbitMQ adds a member on each with no step by a person
- A publisher learns that a message is on a majority of the nodes only through publisher
  confirms, and a consumer keeps it until processed only with manual acknowledgements. Both
  are the application's code; the guide says so and gives the settings for common clients
- `spec.server` is the service's home: the backup and restore-drill timers, the converge of
  virtual hosts and users, and `cloudfall backup run` execute there once, not on every node

Streams are not part of this kind: their replication uses a port range of its own that the
role does not open. `audit` lists a stream queue as unsupported.

### Adding, replacing and losing a node

- **Two more nodes** declared on empty servers join by the converge, one at a time: the role
  starts each with the cookie and the node list, waits until the cluster lists it as
  running, and then waits until the service is healthy
- **Healthy** means: every declared node is running and a cluster member, no node has a
  memory or disk alarm, no partition is reported, and every quorum queue has a leader and as
  many members online as the service has nodes. The role waits up to one hour for it and
  then stops with what is missing; a later converge resumes the wait
- **With one node of three down**, the other two keep every quorum queue and every
  definition writable. A client connected to the lost node reconnects to another; `audit`
  reports the node as missing. When it returns, it catches up with no converge
- **With two nodes of three down**, the node that is left serves nothing that needs a
  majority: no publish to a quorum queue, no change of a definition. `audit` reports the
  cluster without majority, and a converge stops with `PRECONDITION`. Nothing in a converge
  forces the last node to continue alone: that drops whatever only the other two held
- **A node whose server is lost** stays a member of the cluster. Replacing it is naming the
  new server in `nodes` instead of the old one: the converge removes the membership of the
  server that is no longer declared and not running (`forget_cluster_node`), the new node
  joins, and reconciliation gives it a member of every queue. The membership of a node that
  is still declared, or that the cluster sees as running, is never removed
- **A declared node that comes back empty** (same hostname, new volume) is detected: its
  data directory is empty while the cluster holds its membership. The role removes that
  membership and lets it join as a new node. A node that holds data is never emptied
- With all three nodes lost, the definitions are in the backup and the messages are gone

### What each server needs

The kind yields S-011 requirements per node, so nothing below is prepared by hand:

| Requirement | Value |
|---|---|
| `package` | `rabbitmq-server` at `version`, and the Erlang packages of the Debian release |
| `volume` | `rabbitmq`, 20G unless overridden, `mirrored` for one node and `expendable` for three (the copies are on the other servers), mounted at `/var/lib/rabbitmq`, needed by `rabbitmq-server.service`; a node that was alone keeps its mirrored volume when the service grows (D-18) |
| `port` | with three nodes, the node discovery port and the inter-node port from the other nodes only; with `clientAccess`, the TLS client port from `allowedSources` only |
| `certificate` | with three nodes or `clientAccess`, one per server from the project CA, usage server and client, names: the server's hostname and addresses |
| `secret` | `generate`: the Erlang cookie the nodes and the command-line tools share, landing as `/var/lib/rabbitmq/.erlang.cookie`, `0400`, owned by `rabbitmq` |

The ports are RabbitMQ's defaults: 5672 (AMQP 0-9-1 and 1.0) on loopback, 5671 for the same
over TLS, 4369 for node discovery (epmd), 25672 between nodes, 15692 for metrics on
loopback. `bind.port` is the loopback AMQP port. The command-line tools reach the node over
loopback and need no rule.

The role writes the managed settings to files of its own in `/etc/rabbitmq/conf.d`
(listeners, the memory watermark, the node list, the queue defaults, TLS, the metrics
endpoint) and owns `/etc/rabbitmq/enabled_plugins`, which names the Prometheus plugin and no
other. Other settings are the operator's, in files of their own that sort before the managed
ones; `audit` compares only the managed files. Two limits are set where the declaration
meets the host:

- the role refuses, before changing anything, a `memoryHighWatermarkMb` above six tenths of
  the server's memory, which is the share RabbitMQ takes by default
- `disk_free_limit` is 2 GB, not RabbitMQ's 50 MB: below it the node stops taking messages
  while there is still room to drain the queues

Between nodes every connection is encrypted and verified against the project CA in both
directions, and the command-line tools use the same certificates. With one node and no
`clientAccess` nothing listens outside loopback and no certificate is issued.

One server holds at most one RabbitMQ node: the Debian package runs one instance per host.

### Access

RabbitMQ has no login by unix socket, so there is no `application` form (S-006) here: every
account has a password.

- **Cloudfall has no account.** The role, the backup script and the drill use the
  command-line tools, which the Erlang cookie authorises on the node's own server
- **`guest`** is removed: no account exists with a known password
- **A virtual host with `owner`** belongs to a user of that name whose password comes from
  `passwordRef`. The controller computes RabbitMQ's salted SHA-256 hash and only the hash
  reaches the nodes (D-9), in a definitions file the role imports and removes; the salt is
  stored and reused, so a second converge changes nothing (D-25). Definitions live in the
  cluster, so one import on `spec.server` creates the user on every node
- Each owner may configure, write and read everything in its own virtual host and has no
  permission elsewhere. It has no tag, so it can't manage users, policies or the cluster
- The default virtual host `/` is removed with `guest` on a node Cloudfall installed; on a
  node that already holds queues in it, it is left alone and listed as unmanaged
- A virtual host or user that Cloudfall created and that is no longer declared is not
  deleted: the user's password is cleared, so it can't log in, and `audit` lists both as
  undeclared. Users and virtual hosts Cloudfall did not create are left alone and listed as
  unmanaged. Deleting a virtual host deletes its queues and messages, and is never a side
  effect of a converge

Without `clientAccess`, each node answers clients on loopback only, in plain text. With it,
each node also listens on the IPv4 address its `Server` declares, over TLS only (5671),
admitted from `allowedSources` by nftables; a plain connection from outside the host is
refused. An application lists the three nodes in its client and uses whichever answers
(S-021 has no counterpart here).

The management plugin is not enabled, so there is no web interface and no HTTP API. An
operator reads the state of the broker with `audit`, the dashboard and the command-line
tools.

### Backups and the restore drill

`backup` keeps its contract: a schedule, a retention, an optional drill, receipts from
`cloudfall backup run` and `cloudfall backup verify`. Both run on `spec.server`.

- `cloudfall-rabbitmq-backup` exports the definitions of the whole cluster with `rabbitmqctl
  export_definitions` into `backup.directory`, fails unless the file parses and names every
  declared virtual host, and deletes exports older than `retentionDays`. The file holds
  password hashes, so it is `0600` and root-owned
- **A backup holds no message.** The receipt says so in a field of its own, and the guide
  says it first. A message survives a lost server through its copies on the other two nodes,
  and a single node offers nothing beyond its mirrored volume
- `cloudfall-rabbitmq-restore-check` starts a second, empty RabbitMQ node on `spec.server`,
  with a node name, data directory, configuration and loopback ports of its own and no node
  list, imports the newest export into it, compares the virtual hosts, users, queues,
  exchanges, bindings and policies it then reports with the export, and stops it and removes
  its directory. It fails when there is no export and on any difference
- The scratch node never joins the cluster, takes a small fixed memory watermark, and is
  refused a start when the server has less free memory than that
- `backup.directory` is on the server that also holds the data, as for the other kinds, so
  it protects against a deleted queue or virtual host and not against a lost server; an
  off-server copy of the directory is what protects the definitions against that

### Validation

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- `rabbitmq` on another kind, or its absence on this one
- `version` that is not `X.Y.Z` of a supported series (`service_rabbitmq_version`)
- `nodes` without `spec.server`, with an undeclared server or a duplicate, or with a number
  of servers other than one or three (`service_rabbitmq_nodes`)
- a server that is a node of two RabbitMQ services (`service_rabbitmq_server_shared`)
- a node in another environment than the service (`service_rabbitmq_environment`)
- two nodes of a service with the same hostname (`service_rabbitmq_hostname`)
- `remoteAccess` on this kind (`service_rabbitmq_remote_access`)
- a virtual host without `owner` or `passwordRef`, or with `application`
  (`service_rabbitmq_vhost`)
- an `owner` named `guest`, a virtual host named `/`, and either with the scratch prefix
  `cloudfall_` (`service_rabbitmq_reserved`)
- with three nodes or `clientAccess`, a server whose baseline declares no firewall
  (`service_rabbitmq_unfirewalled`), or that has no IPv4 for the others to reach and admit
  (`service_rabbitmq_server_unaddressed`)
- `clientAccess` with a source of every address (the S-006 rule)
- a server whose baseline declares no storage pool or several, or, for one node, a pool
  without a secondary physical volume: the S-014 codes

It warns `RABBITMQ_SINGLE_NODE` for a production environment with one node: one copy of
every message, and every restart stops the service.

### Audit and metrics

- `audit` reports drift when a node's installed version differs from `version`, when
  `rabbitmq-server.service` is not enabled and running, when a managed file differs from the
  rendered one, when a plugin other than the managed one is enabled, when a listener is on
  an address the declaration does not name, and when a requirement of a server is unmet
  (S-011)
- It reports the state of the cluster: a declared node that is not a running member, a
  member that is not declared, a cluster without majority, a partition, a memory or disk
  alarm, a quorum queue with fewer members online than the service has nodes, a durable
  classic queue, a stream queue, and a stable feature flag that is not enabled
- With `metrics.enabled`, the collector on each node scrapes the endpoint the Prometheus
  plugin serves on loopback, with the service id as the label the other exporters use. The
  services dashboard (S-016) gains RabbitMQ panels: messages ready and unacknowledged,
  publish and deliver rates, connections, memory against the watermark, free disk against
  the limit, and quorum queue members online

### Documentation

`docs/rabbitmq-service-guide.md` opens with what a backup does not hold, then covers
declaring one node, growing to three, what an application lists in its client, publisher
confirms and manual acknowledgements, which queues are copied and which are not, a version
change inside a series and to the next one, replacing a lost node, what a team sees while
the cluster has no majority, and restoring definitions from an export.

Touches `config/schemas/v1/service.schema.json`, `config/examples/services/`,
`sdk/src/cloudfall/domain.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/requirements.py`,
`sdk/src/cloudfall/secrets.py`, `sdk/src/cloudfall/audit.py`,
`engine/src/cloudfall_engine/ansible_inventory.py`, a new role
`engine/ansible/roles/cloudfall_rabbitmq`, `engine/ansible/playbooks/services.yml`,
`engine/ansible/playbooks/backup.yml`,
`engine/ansible/roles/cloudfall_logging_collector/templates/config.alloy.j2`,
`engine/ansible/roles/cloudfall_logging_backend/files/dashboards/cloudfall-services.json`,
ROADMAP.md, ARCHITECTURE.md, and the guide.

## Acceptance criteria

- S-023-1: A project with three `Server` resources, their baselines, and the declaration
  above passes `cloudfall config validate` with exit 0, and `cloudfall-engine inventory
  render` carries the service with its three nodes, its virtual host and no password value
- S-023-2: For the project in `config/examples` without a RabbitMQ service, the rendered
  inventory is byte-identical before and after the change
- S-023-3: After `services.yml` converges a service with one node and no `clientAccess` on a
  fresh Debian 13 host, `rabbitmqctl version` reports the declared version, the RabbitMQ and
  Erlang packages are held, the memory watermark is the declared one and the disk limit is
  2 GB, the data directory is on the `rabbitmq` volume, no listener of RabbitMQ or epmd
  exists on a non-loopback address, the only enabled plugin is the Prometheus one, and
  neither `guest` nor the virtual host `/` exists
- S-023-4: A second converge changes nothing (`changed=0`) and restarts no node; a
  `memoryHighWatermarkMb` above six tenths of the host's memory stops the converge before
  any task changes the host
- S-023-5: `jobs` connects on loopback with its password, declares a durable queue with no
  type in `jobs`, and the queue is a quorum queue; an exclusive queue it declares is a
  classic queue; `jobs` is refused on another virtual host, and a login as `guest` is
  refused
- S-023-6: The password of `jobs` and the Erlang cookie appear in none of the rendered
  inventory, playbook output, command answers or receipts; the definitions file that sets
  the password holds a hash and never the password, and is gone after the converge; a
  `passwordRef` whose file or key is missing stops the converge with `PRECONDITION` before
  any playbook runs
- S-023-7: Removing a declared virtual host and converging leaves the virtual host, its
  queues and their messages in place, the user can no longer log in, and `audit` lists both
  as undeclared; a user created by hand is left alone and listed as unmanaged
- S-023-8: Converging the declaration above on three hosts gives one cluster of three
  running nodes; a durable queue declared on `h1` has three members; a message published to
  it through `h1` with a publisher confirm is consumed through `h3`
- S-023-9: Each server's ruleset admits ports 4369 and 25672 from the other two nodes only;
  a connection between nodes is encrypted and verified against the project CA; a node
  started with another cookie, or with a certificate from another CA, does not join
- S-023-10: Adding two nodes to the service of S-023-3 and converging gives the cluster of
  S-023-8 with every queue and message the first node held, each quorum queue grown to three
  members with no step by a person, and no queue redeclared; a second converge changes
  nothing
- S-023-11: With `h2` stopped, a publish with a confirm to a quorum queue through `h1`
  succeeds and `audit` reports the node as missing; when `h2` returns it holds those
  messages with no converge. With `h2` and `h3` stopped, the same publish is not confirmed,
  `audit` reports the cluster without majority, and a converge stops with `PRECONDITION`
  without restarting `h1`; when a node returns, publishing works again with no step by a
  person
- S-023-12: Changing `version` to a later patch of the same series on the cluster restarts
  one node at a time with `spec.server` last, a publisher that uses confirms and reconnects
  to another node loses no message, and every node reports the new version
- S-023-13: Changing `version` from `4.3` to `4.4` does the same as S-023-12 and leaves every
  stable feature flag enabled; an older `version`, and a series before the installed one,
  are refused before any package changes
- S-023-14: With `h2` destroyed, replacing it by an empty `h4` in `nodes` and converging
  gives a cluster of `h1`, `h3` and `h4`, every quorum queue with three members and every
  message, and no membership of `h2` left; `h1` and `h3` took publishes throughout
- S-023-15: A declared node whose volume was recreated empty joins again by one converge and
  receives a member of every quorum queue; a node that holds data and can't join stops the
  converge with nothing removed
- S-023-16: `cloudfall backup run rabbitmq-main` writes one export, `0600` and root-owned,
  and a `BackupReceipt` that states the backup holds no messages; `cloudfall backup verify
  rabbitmq-main` imports the export into a scratch node, finds the same virtual hosts,
  users, queues, exchanges, bindings and policies, removes the scratch node and its
  directory, and writes its receipt; with no export it fails and says so; the scratch node
  is never a member of the cluster, and no process or listener of it is left
- S-023-17: Exports older than `retentionDays` are gone after the next backup run, and the
  newest is kept
- S-023-18: Each refusal listed under Validation fails `cloudfall config validate` with
  `CONFIG_INVALID` and its code; `version: 4.2.9` and `version: 4.5.0` each fail with
  `service_rabbitmq_version`; `nodes` of two servers fails with `service_rabbitmq_nodes`; a
  version of a series within 90 days of its end of support validates with the warning
  `RABBITMQ_VERSION_ENDING` naming the date; one node in a production environment validates
  with `RABBITMQ_SINGLE_NODE`
- S-023-19: With `clientAccess` of `allowedSources: [10.0.0.0/24]`, `jobs` connects to each
  node from an allowed address on 5671 verifying the project CA, a plain connection to 5672
  from that address is refused, and each ruleset holds `ip saddr 10.0.0.0/24 tcp dport 5671
  accept` and no unrestricted rule for a RabbitMQ port
- S-023-20: `cloudfall audit` reports drift when the installed version differs from the
  declaration, when the unit is stopped, when a managed file is changed by hand, when
  another plugin is enabled, when a durable classic queue or a stream queue exists, and when
  a quorum queue has fewer than three members online on a three-node service
- S-023-21: With `metrics.enabled`, the rendered collector configuration of each node holds
  one scrape of its RabbitMQ endpoint, labelled with the service id, and none without it;
  the services dashboard shows the messages ready of the service and the members online of
  its quorum queues

## Out of scope

- A backup of messages, by any means: a copy of a stopped node's data directory, shovels to
  a second cluster, or a standby cluster
- Two nodes, and more than three
- Removing a node without replacing it, and going from three nodes to one
- Forcing a node without majority to continue alone
- Streams and the stream protocol
- The management plugin, its web interface and its HTTP API
- MQTT, STOMP, their WebSocket forms, federation, shovel, and every other plugin
- Users with tags, users with access to several virtual hosts, and permissions narrower
  than a whole virtual host
- Exchanges, queues, bindings and policies in the declaration: the application declares its
  own topology
- A pass-through of broker settings in the declaration
- A proxy or load balancer in front of the nodes; the client lists them
- Erlang from another source than the Debian release
- Moving in from a RabbitMQ server Cloudfall does not manage, and from a hosted broker
- Skipping a series, and moving to an older version
- RabbitMQ 3.13 and the 4.x series before 4.3, and so Mnesia as the metadata store
- The commercial edition and its long-term support
- Automatic replacement of a lost server: an operator changes the declaration
- Connections between components and stores with a generated user per connection:
  cloudfall-dev/cloudfall#216
- Deleting a virtual host or a user

## Decisions relied on

- D-9: an owner password reaches the server only as a hash computed on the controller;
  stated for PostgreSQL, applied here to RabbitMQ users
- D-18: a mirrored volume meets an expendable requirement
- D-25: a password hash converges by reusing the stored salt; stated for PostgreSQL, applied
  here to RabbitMQ's salted hash

## Issues

## Verification

No criterion is verified yet.
