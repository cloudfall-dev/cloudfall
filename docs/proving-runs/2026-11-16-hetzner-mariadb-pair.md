# Proving run: MariaDB replication (S-019) and replication-manager with the watchdog (S-020)

| | |
| --- | --- |
| **Date** | 2026-11-16 |
| **Scope** | #441: S-019-12 to S-019-16 on a real primary and replica converged by Cloudfall. #427: replication-manager deciding and a watchdog fencing, on the same pair and a manager host, for S-020-5 to S-020-10 |
| **Hosts** | Three Hetzner Cloud cx23 (fsn1), Debian 13.6, NTP-synchronised; two 12 GB volumes per database host made into `vg_data` by hand. All deleted after the run |
| **Code** | `main` at c32ec5e; MariaDB 11.8.9; `replication-manager-osc` 3.1.48-1 |
| **Verdict** | S-019-14 and S-019-16 held. S-019-12, S-019-13 and S-019-15 did not as built (#449 to #453). Spec 020's design held on safety: no two servers were ever writable at once, and the fence always came before the promotion. The promotion time did not: 107 to 121 s where S-020-7 allows 30 |

Full results, with every time and answer: the S-019 comment on #423 and the S-020 comment on
#417. This file keeps the method and the outcome.

## Method

**S-019.** A project with the two servers, a default-deny baseline with the `data` pool, and the
pair of spec 019 (`billing` with an owner, buffer pool 1024 MB). Secrets were kept in sops with a
throwaway age key. Public entry points: `config validate`, `secrets generate`,
`baseline converge --yes`. Services were converged through `converge_services`, the function
behind the `converge_services` MCP tool, called from a short script so one step could be limited
to one server.

**S-020.** Spec 020 is not built, so its parts were set up by hand to its text: the manager on
a third host with the spec's settings for `fenceSeconds: 10`; `cloudfall_failover` with the
documented grants for MariaDB 10.5.2 and later plus `SLAVE MONITOR` (not `ALL`); and
`cloudfall_watch`. A watchdog unit ran on each database server, its manager witness read from
the server's own processlist. Each server also ran a socket client writing one row a second and
a sampler of `@@global.read_only` four times a second. Cuts were nftables drops in both
directions; the power loss was `hcloud server poweroff`.

## Results

| Criterion | Result |
| --- | --- |
| S-019-12 | Not as built: a new replica on a Cloudfall volume is refused because `lost+found` counts as a database (#449). Everything else held after removing it by hand: seed, GTID threads, `read_only`, row propagation, owner write refused, the `/32` rule, TLS verified against the project CA |
| S-019-13 | Partly: no second seed, but the replica reports `changed` on every converge (#450, #452) |
| S-019-14 | Held: promotion naming the last GTID applied; refusal over a writable source naming both positions |
| S-019-15 | Not as built: a role swap does not end in two whole converges in either host order (#451, #450); every role change also needs a baseline converge for its port (#453) |
| S-019-16 | Held: divergence reported with the GTIDs, nothing attached or dropped |
| S-020-5 | Held: back read-only, writable 1.1 s after the restart |
| S-020-8 | Held: no promotion while the replica still heard the primary |
| S-020-9 | Held with the IO thread stopped: fence 9.2 s after, promotion 24.5 s after the fence. With both threads stopped, nothing is promoted |
| S-020-6 | Never two writable; fence after 12.6 s (the criterion says 12; the hand-built watchdog's peer probe was the cause); promotion 107 s after the fence |
| S-020-7 | Never two writable; new primary writable about 121 s after the power-off, against the 30 s allowed |
| S-020-10 | Held: 10 minutes, 591 writes, no fence |

## Findings

- replication-manager's detection costs `db-servers-connect-timeout` (5 s) per ping against a
  host that drops packets: ten pings took 68 to 69 s, where the bench saw 20. The spec's
  waits should be computed with the connect timeout
- In S-020-6 and S-020-7, about 50 s passed between the election and `STOP SLAVE` on the
  candidate; not in S-020-9, where the candidate's IO thread was already stopped. The cause
  was not visible at log level 1
- The manager listens on `*:2003`, `*:2004`, `*:10002` and `*:7002` besides the loopback API
- The manager takes one replication password; Cloudfall gives each primary its own

## Caveats

- Watchdog, manager configuration and firewall rules for the manager were hand-made to the
  spec's text, not Cloudfall code; the rules did not survive a reboot, so S-020-12 was only
  partly seen
- One run of each case, on one Debian release and one MariaDB version
