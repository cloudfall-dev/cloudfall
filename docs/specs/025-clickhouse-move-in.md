# S-025: Moving into a ClickHouse service

status: approved
serves: G-2, G-3

## Problem

S-022 (spec PR #428, build issue #429) lets a project declare a ClickHouse `Service`: one or
two replicas, an exact version of a maintained LTS series, every declared database on the
`Replicated` engine, Keeper inside the service, and no `default` user. It leaves out moving in
from a ClickHouse that runs elsewhere (cloudfall-dev/cloudfall#536). A cluster run by hand
differs from that on every point S-022 fixes:

- more than two replicas, with replica sets that differ per table (tables on 2, 3 and 4
  servers under one shard name set by macros)
- versions out of support and mixed across servers (23.10 and 24.9 side by side)
- `Atomic` databases holding `ReplicatedMergeTree` tables with their own literal Keeper paths,
  not `Replicated` databases
- Keeper built into the servers with an even number of voters on plain ports (9181), and the
  `default` user in use by applications

Today such a team builds a new service beside the old cluster and copies the data by hand,
and nothing in Cloudfall proves the copy is complete. A built-in migration is the wrong
answer for the reason S-013 gave for Elasticsearch: sources differ too much to test. So this
spec follows the shape of S-013: a guide an agent builds the move from, and a read-only check
that proves it. A move the agent generated and then reports as done is its own witness, and
G-2 asks for a verify step on every operation.

This spec depends on S-022 and its build (#429), and on S-013's `cloudfall data check`
(cloudfall-dev/cloudfall#232), which it extends to a second kind.

## Behaviour

### The guide

`docs/clickhouse-migration-guide.md` is written for an agent working in a Cloudfall project
that declares the target service (S-022). It covers, in order:

1. **Read the source**, read-only, with `SELECT` and `SHOW` only: the version of every
   server, each database with its engine, each table with its engine, its replica set (the
   servers that hold it, from `system.replicas`), its Keeper path, its sorting and partition
   keys, its rows and its size on disk; materialized views and the tables they write to;
   dictionaries; users, roles and grants, and which application logs in as which user
   (`default` included); which services write and how often
2. **Map it onto the declaration**: one declared database per source database, under the
   same name, with an `owner` per database (S-022 Access). A source database whose name
   S-022 reserves (`default`, `system`, names starting with `cloudfall_`) is renamed in the
   application before the move; the guide says so and the check does not rename
3. **Choose the path**:

   | Source | Path |
   |---|---|
   | Any version the target's home server can reach over ClickHouse's TLS native or HTTP port | Copy into the declared service with `INSERT ... SELECT FROM remoteSecure()`, run on `spec.server`, one table and one partition at a time |
   | Not reachable from the target, or a backup as the only access | The source's operator takes a `BACKUP`; it is restored on a temporary ClickHouse server of the source's own version, outside the service, and copied from that server by the first path |
   | Joining the source's cluster (the target's servers as replicas of its tables in its Keeper) | Not a path (Out of scope) |

4. **Create the schema on the target**, in the declared `Replicated` database, from each
   source table's `SHOW CREATE TABLE` rewritten: the engine's Keeper path and replica
   arguments removed, so `ReplicatedMergeTree('/clickhouse/tables/{shard}/db/t',
   '{replica}')` becomes `ReplicatedMergeTree` and the `Replicated` database chooses the
   path; `ON CLUSTER` and macros removed; columns, keys, TTL and table settings kept. The
   owner creates them on `spec.server` and the database applies them on the other replica.
   Materialized views and dictionaries are created after the load, without `POPULATE`, so a
   copied insert does not run them a second time; the tables they write to are copied like
   any other table
5. **Generate the move** as a playbook in the project (below): partitions copied oldest
   first, a rate limit (`max_threads`, `max_insert_threads`, one partition per statement),
   and a partition that failed half way dropped on the target and copied again, never
   appended to
6. **Keep up with writes** (below)
7. **Switch and soak** (below): the source stays current and reachable until the check has
   passed after the last write and the soak has ended

Its rules, stated as rules:

- the source is read-only: Cloudfall and the generated playbook change nothing on it. What
  the source needs (an account, a firewall rule, a `BACKUP`) is its operator's step, which
  the guide lists, as section 8 of `docs/mariadb-service-guide.md` does
- the generated playbook targets the declared service's servers and, on the backup path, the
  temporary server; never a server of the source
- it is declared as an operation in the project's `operations/` catalog with risk
  `destructive` and runs through `cloudfall-engine playbook run` with the rendered
  inventory, behind the same approval as any change to a server (D-20); a person running
  it by hand runs that same command
- credentials of the source are read from a file a person provides, `0600`, never from a
  command line or a resource; on the target they reach ClickHouse as a named collection in a
  `0600` file of the playbook's own in `config.d`, sorting before the files S-022 manages,
  and removed when the move ends, so no query text holds them
- the move is done when `data check` says so, not when the playbook ends

### Writes during the copy

ClickHouse has no change stream to follow, so Cloudfall builds no catch-up: as S-013 did,
it leaves that to the application and the guide. The guide gives two ways and what each
misses:

- **A final pass on the open partitions** (the default): closed partitions are copied while
  the source still takes writes; then the writers stop, every partition that changed since
  its copy (by `system.parts` modification time on the source) is dropped on the target and
  copied again, and the check runs. The write stop lasts as long as copying the open
  partitions
- **Double writes**: the application writes to both before the load, and the load copies
  only rows before a cutoff on a time column. It needs a cutoff column on every table and
  misses mutations (`ALTER ... UPDATE`, `DELETE`) on the source during the load

Row counts catch a missing or doubled row, not a changed one; the guide says that a table
mutated during the load gets the final pass whichever way was chosen.

### The check

```console
cloudfall data check <service> --source-url-file <file> [--table <database>.<pattern>]...
```

This is S-013's command, extended to a service of kind `clickhouse`. What S-013 says of it
holds unless named here.

- `<service>` is a declared `Service` of kind `elasticsearch` or `clickhouse`; another kind
  fails with `PRECONDITION` (`data_check_kind_unsupported`). `--table` with an Elasticsearch
  service and `--index` with a ClickHouse service are refused as usage errors naming the
  flag
- The file is `0600` and holds one URL per line, one per server of the source, each with its
  credentials, for ClickHouse's HTTP interface (`https://user:password@host:8443`). The
  target is reached on every replica through its own server, with Cloudfall's own
  `cloudfall` account on loopback (S-022 Access)
- Without `--table` it selects every table in every source database that has the name of a
  database the service declares; with it, the tables matching any pattern. A declared
  database absent on the source, and a pattern that matches nothing, fail with
  `PRECONDITION`. Tables of an engine that holds no rows (views, `Distributed`, `Merge`,
  `Null`, `Kafka` and other integration engines) are listed as `skipped` and do not decide
  the verdict; a materialized view with no `TO` table is compared by the table that holds
  its rows
- Per selected table it compares: presence on the target, the row count on every replica
  that holds it on each side, and the schema. The schema is the columns in order with their
  types, the sorting key and the partition key. The engine (replicated or not), the Keeper
  path, the database engine and table settings are no difference; nor is a type the
  target's version only spells differently, from one table of such pairs in the SDK
- Every query it sends to either side is a `SELECT` or `SHOW` with the setting `readonly=1`,
  so the server refuses anything else; an account that holds `SELECT` on the selected
  databases and `SHOW` on `system` is enough on the source. It runs no `SYSTEM` statement and
  changes neither side: no database, table, row, user, grant or Keeper node is created,
  changed or removed
- Every selected table matching answers `data.status` `ok` and exit 0. Any difference
  answers `drift` and a non-zero exit, with each differing table and the kind of difference:
  `missing`, `count` (with the counts of both sides), `schema` (with the differing columns
  or keys), or `unsettled` when the replicas of one side disagree with each other (with each
  replica's count); a table still taking writes or still replicating reads as `unsettled`
  until it settles
- It writes a `DataCheckReceipt` beside the receipts `data copy` writes (D-16): the service,
  the source's hosts without credentials, the time, each table with every replica's count
  on both sides and its verdict, the skipped tables, and the overall verdict
- The source's URLs and credentials appear in no answer, log line or receipt
- A source server that cannot be reached or refuses the credentials, and a target replica
  that cannot be reached, fail with `PRECONDITION` and write no receipt

### How applications switch

- An application lists both replicas of the service in its client (S-022 Access, with
  `clientAccess` when it runs on another server) and logs in as the owner of its database,
  with the password from the declaration's `passwordRef`. Both replicas take writes, so
  there is no primary to switch to
- Users and grants on the target are the declared ones only. The source's users, roles and
  grants are not copied; the guide maps each source user an application uses onto a
  declared owner, and names the users that have no declared form (read-only users, row
  policies, quotas: out of scope in S-022)
- The source's `default` user is not carried over: an application that logs in as `default`
  is given an owner and its password before it moves, and on the target a login as
  `default` is refused (S-022-3)
- Reads move first, one instance at a time; writes move after the final pass and a passing
  check; the source stays reachable and unchanged through the soak. Retiring it is its
  operator's step

### The example

`examples/clickhouse-move-in/` holds a working project of the copy path that the guide builds
from: a declared pair (S-022), the operation `operations/clickhouse-move-in.yml` with risk
`destructive` and its verify playbook, and the playbook. The playbook's first play asserts
the rendered Cloudfall inventory is loaded and holds the target service, and that no host of
the source is in the play; anything else stops it before a task changes a host.

Touches `sdk/src/cloudfall/app.py` (`data check`), `sdk/src/cloudfall/data_check.py`,
`config/schemas/v1/data-check-receipt.schema.json`, `docs/clickhouse-migration-guide.md`,
`docs/clickhouse-service-guide.md` (a pointer to the migration guide), and
`examples/clickhouse-move-in/`.

## Acceptance criteria

- S-025-1: Against a source of two servers and the S-022 pair holding the same three tables,
  `cloudfall data check clickhouse-main` exits 0 with `data.status` `ok` and writes a
  `DataCheckReceipt` with three matching entries, each with the row count of every replica
  on both sides
- S-025-2: After one row is deleted on one target table, it exits non-zero with `drift` and
  names that table, the kind `count` and both counts; with a table absent on the target the
  kind is `missing`; with a column of another type, or another sorting key, the kind is
  `schema` and the column or key is named
- S-025-3: A source table of version 23.10 in an `Atomic` database with
  `ReplicatedMergeTree` and a literal Keeper path, against the same table in the target's
  `Replicated` database with the engine's replicated form and no path argument, checks as
  `ok`
- S-025-4: With the two replicas of one source table holding different counts, the check
  exits non-zero with `drift`, the kind `unsettled` and each replica's count
- S-025-5: A view and a `Distributed` table in a selected database are listed as `skipped`
  and the check still exits 0 when every other table matches
- S-025-6: With a source account that holds only `SELECT` on the selected databases and
  `SHOW` on `system`, the check runs; the source's `system.query_log` lists only `SELECT`
  and `SHOW` queries from that account, each with `readonly=1`; the tables, row counts,
  users, grants and Keeper paths of both sides are the same before and after a run
- S-025-7: The source's URLs and credentials appear in none of the answer, the receipt, the
  process arguments or the log output
- S-025-8: An unreachable source server, refused credentials, an unreachable target replica,
  a declared database absent on the source, and a `--table` pattern matching nothing each
  fail with `PRECONDITION`, and no receipt is written
- S-025-9: `--table` with an Elasticsearch service and `--index` with a ClickHouse service
  are refused as usage errors naming the flag; a service of kind `mariadb` fails with
  `PRECONDITION` and `data_check_kind_unsupported`
- S-025-10: `docs/clickhouse-migration-guide.md` holds the path table, the five rules, the
  two ways of keeping up with writes, and the switch with the `default` user not carried
  over; `docs/clickhouse-service-guide.md` points to it
- S-025-11: The example playbook run with an inventory not rendered by Cloudfall, or with a
  host of the source among its targets, fails its first play and changes no host;
  `cloudfall operations` lists the example's operation with risk `destructive` and a verify
  playbook
- S-025-12: Through `cloudfall-engine playbook run`, the example playbook moves a source of
  two 24.x servers (an `Atomic` database, two `ReplicatedMergeTree` tables, one materialized
  view with a `TO` table) into the S-022 pair: each table exists on both replicas with the
  replicated engine and no literal Keeper path, the view's table holds no doubled rows, the
  source's tables, rows and users are unchanged, and `data check` exits 0
- S-025-13: After the example's move, `system.users` on each target replica holds only
  `cloudfall` and the declared owners, a login as the source's `default` is refused, and a
  client given both replicas writes as the owner through either
- S-025-14: A proving run recorded in `docs/proving-runs/`: an agent given only the guide and
  a source of three servers on mixed versions, with per-table replica sets, `Atomic`
  databases and Keeper on 9181, moves it by the copy path into a pair with a final pass
  under a short write stop; given only a backup of a source, by the backup path through a
  temporary server; each ends with a passing `data check`

## Out of scope

- Joining the source's cluster: declaring the target's servers as replicas of the source's
  tables in its Keeper, the analogue of section 8 of `docs/mariadb-service-guide.md`.
  S-022's Keeper serves its own service only, its databases are `Replicated` with the shard
  named after the service, and a database it did not create on that engine is refused; a
  joined replica would end the move holding `Atomic` tables on the source's Keeper paths,
  and a source out of support is too many versions away to replicate with
- Converting an `Atomic` database to `Replicated` in place, on either side
- Restoring a source's `BACKUP` directly into the declared service: it recreates the
  source's tables with their own Keeper paths, which S-022's drill refuses
- A built-in `data copy` for ClickHouse, and catch-up machinery (change capture, double-write
  proxies): the application's and the guide's matter
- Comparing row contents or checksums; presence, row counts and schema are the check
- Renaming databases or tables during the move
- More than two replicas or more than one shard in a declared service (S-022 sets those)
- Carrying over the source's users, roles, grants, row policies, quotas and settings
  profiles
- Running or scheduling the generated playbook beyond `cloudfall-engine playbook run`
- Sources other than ClickHouse, ClickHouse Cloud included

## Decisions relied on

- D-16: the check's receipt is a record and lives beside `data copy`'s receipts under
  `record/`, holding no secret value
- D-20: the generated playbook is approved as proposed, its sha256 bound to the approval

## Issues

## Verification

Filled in when the spec reaches `built`: one line per criterion id, saying how it was
checked against the default branch (beyond its tests) and the result.
