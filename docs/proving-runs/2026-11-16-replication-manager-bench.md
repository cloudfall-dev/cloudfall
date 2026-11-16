# Bench run: replication-manager on a MariaDB pair without a proxy

| | |
| --- | --- |
| **Date** | 2026-11-16 |
| **Scope** | Whether replication-manager can be the deciding part of S-020: a primary and one asynchronous GTID replica, no proxy, clients that ask the servers for `read_only` (S-021) |
| **Hosts** | None: containers on one Docker 29.9 host (linux/arm64). `mariadb:11.8` (11.8.9) twice, `signal18/replication-manager:v3.1.49` under amd64 emulation |
| **Code** | No Cloudfall code. The bench's own scripts, the watchdog below among them |
| **Verdict** | The manager promotes without a proxy and opens writes on its primary by itself. Alone it lets a cut-off primary keep taking writes; with a watchdog on the database host and the manager's two waits raised, no run had two writable servers |

A bench, not a proving run: no criterion of S-020 is proven by it. Times come from an
emulated manager and a one-second sampling loop, so they are good to a second or two.

## Method

Three containers on three networks, one per link (manager to each server, and server to
server), so each link is cut by disconnecting a container from one network. A fourth
network pair carries a remote client.

- `db1` and `db2`: `log_bin`, `binlog_format=ROW`, `log_slave_updates`, `gtid_strict_mode`;
  `db2` replicates from `db1` with `MASTER_USE_GTID=slave_pos` and is read-only
- the manager: one cluster of the two servers, `failover-mode = "automatic"`,
  `autorejoin = false`, `force-slave-heartbeat = true`, no proxy configured, every other
  setting at its default unless a run says otherwise
- a local writer inside `db1`'s container, one row a second through the unix socket: the
  application on the primary's own host
- a remote client: every second it asks both servers for `read_only`, writes to the one
  that is writable, and logs when it sees two or none

## Results

| Run | Watchdog | Manager settings | What happened |
| --- | --- | --- | --- |
| Primary killed | no | defaults | Promoted 9 s after the kill (five pings two seconds apart); the remote client wrote to the new primary 10 s after |
| Killed primary started again | no | defaults | It came up writable; the client saw two writable servers for about 2 s, until the manager set it read-only. With `autorejoin` off the manager left it standalone |
| Primary cut off from manager, replica and client, local writer running | no | defaults | Split: the replica was promoted after 9 s and the old primary stayed writable. 51 rows went to the old primary only, until the cut ended 52 s later and the manager set it read-only |
| The same | 8 s | ping counter 10 | The watchdog turned writes off 9 s after the cut; the manager promoted 19 s after it. Never two writable servers. 9 rows written before the fence are on the old primary only |
| Primary cut off from the manager only | 8 s | ping counter 10 | No promotion: the manager reported that the replica still received from the primary. The primary stayed writable, its witness being the replica |
| The same, then the replica's link stopped by hand | 8 s | ping counter 10 | The watchdog fenced 8 s after the stop and the manager promoted 10 s after it: a margin of 2 s. 8 rows on the old primary only |
| Primary with `read_only=ON` in its configuration restarted | no | ping counter 10 | Back and read-only after 4 s; the manager turned its writes on in the same second it saw it. The replica reconnected only after MariaDB's default 60 s retry |

## What follows for S-020

- No proxy is needed for the manager to work; it logs a two-second wait for an unmanaged
  proxy and goes on
- The wait before a promotion needs no script. Two documented settings give it:
  `failover-falsepositive-ping-counter` for a primary that stops answering, and
  `failover-falsepositive-heartbeat-timeout` for a lost link. The sixth run left the second
  at its default and got a two-second margin, so both must follow the fence time
- A server that starts read-only fits the manager: it opens writes on its primary
- The watchdog bounds the loss to the fence time; it does not remove it
- A replica's connect retry should be far below sixty seconds

## Second part: the package, the grants, the witness

Run the same day, with the manager installed from Signal18's package instead of its image.

- **Installation.** `https://repo.signal18.io/deb`, component `3.1`, serves
  `replication-manager-osc` for bookworm and trixie on amd64 and arm64; checked in clean
  `debian:12` and `debian:13` containers of both architectures. `=3.1.48-1` installs and
  holds, and versions back to 3.1.43 are there; 3.1.49, two days old, was not yet. The
  signing key is `AA79 F8FA 35BF E809 9FD7 4A76 28A4 86AA FAE2 0E50`. The package depends
  only on `replication-manager-client`, is 210 MB to download, installs the unit
  `replication-manager.service` running `replication-manager-osc --user repman monitor`,
  creates the user `repman` with a shell and an `.ssh` directory, and ships
  `/etc/replication-manager/config.toml` as `0644`
- **The API.** `api-bind` defaults to `0.0.0.0`, and the default login `admin:repman` is
  accepted on a fresh install
- **Grants.** With the list from the manager's documentation for MariaDB 10.5 and later,
  the manager could not read the replica's status on 11.8.9 (`Access denied; you need the
  SLAVE MONITOR privilege`), saw no replica, and after declaring the primary failed promoted
  nothing. With `SLAVE MONITOR` added from the start, a read-only primary was reopened two
  seconds after its restart and a killed primary was replaced after 22 s (ping counter 10).
  The manager still logs once that its user "must have SUPER"; nothing failed for it. A
  grant given while the manager is connected does not reach its open sessions
- **The witness.** The manager keeps two sessions on each server and runs statements on
  them every two seconds. Their idle time in `information_schema.processlist` stayed under
  two seconds while the manager reached the server, grew second by second from the moment
  it was cut off (23 s after 23 s), and was back under two seconds after the link returned.
  A watchdog can so tell that the manager reached its server without any way to the manager

## Not covered

- Real hosts, a real power loss, and a cut that passes packets one way only
- A restart of the manager during a promotion
- The unit under systemd: the package was installed in containers and the manager started
  by hand

## The bench watchdog

It only turns writes off. Its witness for the manager is a TCP connection to the manager's
API port, which is weaker than S-020 asks for.

```bash
#!/bin/bash
# Bench watchdog: turns this server's writes off when it has had no witness for FENCE seconds.
# A witness is the manager being reachable, or the peer answering that it still replicates from this server.
last=$(date +%s)
while true; do
  now=$(date +%s); w=""
  if timeout 1 bash -c "</dev/tcp/repman/10005" 2>/dev/null; then w=manager; else
    s=$(mariadb --connect-timeout=1 -h "$PEER" -urepman -p"$REPMAN_PW" -e 'show slave status\G' 2>/dev/null)
    if echo "$s" | grep -q "Master_Host: $ME" && echo "$s" | grep -q "Slave_IO_Running: Yes"; then w=peer; fi
  fi
  [ -n "$w" ] && last=$now
  ro=$(mariadb -uroot -p"$ROOT_PW" -N -B -e 'select @@read_only' 2>/dev/null)
  echo "$(date -u +%H:%M:%S) witness=${w:-none} since=$((now-last)) ro=$ro"
  if [ $((now-last)) -ge "$FENCE" ] && [ "$ro" = "0" ]; then
    mariadb -uroot -p"$ROOT_PW" -e 'SET GLOBAL read_only=1'
    mariadb -uroot -p"$ROOT_PW" -N -B -e "select concat('KILL ',id,';') from information_schema.processlist where user not in ('root','system user','repl','repman','event_scheduler')" | mariadb -uroot -p"$ROOT_PW"
    echo "$(date -u +%H:%M:%S) FENCED"
  fi
  sleep 1
done
```
