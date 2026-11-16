# S-021: An application finds the MariaDB primary by itself

status: approved
serves: G-3

## Problem

S-019 gives a MariaDB primary and a replica whose roles change with the declaration. An
application does not follow: the primary's address sits in its environment, written by hand,
so every switch of roles is also an edit of that environment and a restart of the
application, on every host that runs it (cloudfall-dev/cloudfall#416).

The usual answer is a proxy on each application host. It also takes away the fastest way
in: an application on the primary's own host connects through the unix socket today. This
spec keeps that path and puts the rule in the application. It does not decide who changes
the roles: a person through the declaration (S-019) today, and whatever promotes
automatically later. The rule is the same for both, because it reads only what the servers
say about themselves.

## Behaviour

### What the servers promise

The application relies on two things, both true of an S-019 pair:

- at most one server of a pair takes writes, and each server answers truthfully whether it
  is read-only
- a server that stops being the primary closes its client connections. S-019 makes a
  demoted server read-only; this spec adds the closing, in the same step of
  `cloudfall_mariadb`, so that a statement on a connection opened before the switch fails
  instead of reading from a server that no longer leads

### The contract

The application's environment holds the fixed list of both servers of the pair, and for a
server on its own host the socket path. The list does not change when the roles do. The
rules, as `docs/mariadb-client-guide.md` states them:

1. Connect to the server last known to be writable, through the socket when it is local,
   with a connect timeout and a read timeout set: without them a dead host hangs a request
   for minutes
2. Ask every new connection, and every connection taken from a pool, whether its server is
   read-only, before the first statement. This is how an application that only reads, or
   one whose connection lives for one request, learns of a switch
3. On a connection error, a timeout, a read-only answer to rule 2, or a statement refused
   because the server is read-only, ask each server of the list whether it is read-only,
   take the one that is not, reconnect and run the transaction again
4. When no server is writable, fail the request and try again on the next one
5. When two servers say they are writable, write to neither and fail the request
6. Never write to a server that says it is read-only, and never cache the choice longer than
   the connection lives

The asking happens only on a new connection and after an error: a statement on a healthy
connection costs nothing more than before.

An application under this contract uses an `owner` with a password (S-019): the
passwordless socket form works on the database's own host only. It still connects through
the socket there, with the password. Elsewhere it connects over TCP with TLS and verifies
the server against the project CA, which needs `remoteAccess` on both servers from the
application's hosts.

A transaction in flight at a switch fails. The application runs it again; a wrapper does so
by itself only for a transaction it was handed whole, as one function.

### The wrappers

`examples/mariadb-clients/` holds a reference wrapper for Python, around PyMySQL, and one
for PHP, around mysqli: one file each, with no dependency beyond that driver, implementing
the six rules around a connection factory. They are examples to copy, not published
packages, and CI tests them against a pair the test starts.

- a wrapper given no connect timeout or no read timeout refuses to connect
- the error for "no server is writable" and the one for "two servers are writable" are
  each a type of their own, so an application can tell them from a failed statement
- the Python wrapper can also ask both servers every few seconds in the background, and so
  learn of a switch before any statement fails. The option is off by default; a process
  that lives for one request has no use for it, so the PHP wrapper has none

### Documentation

`docs/mariadb-client-guide.md` holds the contract, what each rule protects against, how to
use the two wrappers, and what stays the application's own: running a failed transaction
again.

Touches `engine/ansible/roles/cloudfall_mariadb`, `examples/mariadb-clients/`,
`.github/workflows/ci.yml`, `docs/mariadb-service-guide.md`, and the guide.

## Acceptance criteria

- S-021-1: After a planned switch of an S-019 pair by declaration, no client session opened
  before the switch is left on the old primary, and the old primary is read-only
- S-021-2: The Python and the PHP wrapper, each writing one row a second through that
  switch, lose no row they were told was committed, report an error for the rows in flight
  at the switch, and write to the new primary within five seconds of its becoming writable,
  with no restart of the client and no change to its environment
- S-021-3: The wrappers connect through the unix socket to a writable server on their own
  host and over TCP with TLS, verifying the project CA, to one elsewhere, and never send a
  write to a server that reports read-only
- S-021-4: A wrapper whose server is powered off mid-request reports an error within its
  read timeout, not later, and a wrapper given no timeout refuses to connect
- S-021-5: After a planned switch with no write in flight, a client that only reads gets an
  error on its next statement on the old connection and reads from the new primary on the
  one after; a client that opens a connection per request reads from the new primary on its
  first request after the switch
- S-021-6: With no server writable, a request fails with the error of that type; when one
  becomes writable, the next request succeeds with no restart
- S-021-7: With both servers made writable by hand, a wrapper writes to neither and fails
  the request with the error of that type
- S-021-8: A transaction handed to a wrapper as one function and cut by the switch is run
  again on the new primary and commits once; a statement outside such a function fails and
  is not repeated
- S-021-9: With background checking switched on, the Python wrapper moves to the new primary
  within its interval with no failed statement in between when no request was in flight;
  with it off, no connection or thread exists beyond the application's own
- S-021-10: On a healthy connection the wrappers send no statement beyond the application's
  own after the first
- S-021-11: The guide holds the six rules, and each wrapper is one file that imports nothing
  beyond its driver and the language's standard library

## Out of scope

- Who changes the roles: S-019 for a change of declaration; an automatic failover is
  cloudfall-dev/cloudfall#416's other half and a spec of its own
- A proxy on application hosts: the answer for an application the team can't change
- Reading from the replica, with or without a primary; a wrapper sends every statement to
  the writable server
- Rendering the list of servers into a component's environment:
  cloudfall-dev/cloudfall#216
- Published client packages, and wrappers for other languages and drivers
- Connection pooling inside the wrappers; they wrap the application's own factory

## Decisions relied on

- none

## Issues

## Verification

No criterion is verified yet.
