# Proving run: a MariaDB pair, and a replica of an outside 10.11 and 11.4 source (S-019)

| | |
| --- | --- |
| **Date** | 2026-11-17, 10:20 to 11:11 UTC |
| **Scope** | #482: the pair checks of S-019-12, S-019-13 and S-019-15 after their fixes (#449 to #453), and S-019-17 and S-019-18 against a MariaDB server Cloudfall did not create, on 10.11 and on 11.4. S-019-21 was read on the pair too |
| **Hosts** | Hetzner Cloud cx23 (nbg1), stock Debian 13: `h1` and `h2` for the pair; `h3` for the outside source; `h4` and then `h5`, each fresh, as the source's replica. Each database host had two 12 GB volumes made into `vg_data` by hand. All deleted after the run |
| **Code** | `main` at 45a0614; MariaDB 11.8.9 on Cloudfall's servers; the source in Docker on `h3` (`--network host`): `mariadb:10.11` (10.11.19), then `mariadb:11.4` (11.4.13) |
| **Verdict** | The pair held: S-019-12, S-019-13 and S-019-15 now pass on real servers, and S-019-21 held. S-019-18 held on both source versions. S-019-17 did not hold as built: a replica of an outside source stops at its server certificate, which no Cloudfall step provides. With that certificate placed by hand, everything else in S-019-17 held on both versions |

## Method

**Pair.** A project with `h1` and `h2`, a default-deny baseline with the `data` pool, and
`mariadb-main` (primary, server 1, `listenAddress` `h1`'s IPv4, databases `billing` with
owner `billing` and `ledger` with owner `ledger` and `authentication: ed25519`) and
`mariadb-replica` (replica, server 2), buffer pool 1024 MB. `cloudfall secrets generate`
stored the replication password; the owner passwords were added to the same sops fragment by
hand (throwaway age key).

**Outside source.** On `h3`, MariaDB in Docker with `server_id = 3`, `log_bin`,
`binlog_format = ROW` and a certificate from a test CA (with `h3`'s IPv4 as its name). It held
`shop.orders` (3 rows), `crm.contacts` (2 rows), a user `appuser@'%'`, and the account of
`docs/mariadb-service-guide.md` section 8: `cloudfall_seed@<replica IPv4>`,
`mysql_native_password`, `REQUIRE SSL`, with `REPLICATION SLAVE, BINLOG MONITOR, SELECT, SHOW
VIEW, TRIGGER, EVENT, RELOAD ON *.*`. A separate project declared one fresh server and
`mariadb-moved`, a replica (server 12) with `source` of `h3`, a `credentialsRef`, and
`caCertificateFile` the test CA. The 11.4 round used a new container with a fresh data
directory and a new fresh replica host, so each round began with no `mariadb-server` on the
replica.

Entry points: `cloudfall config validate`, `cloudfall secrets generate`,
`cloudfall baseline converge --yes` (once per host, before its first services converge), and
`converge_services`, the function behind the MCP tool, from a short script that kept the
playbook output (with `-v` for every converge of the outside source).

## Results: the pair

| Step | Result |
| --- | --- |
| First converge | `seed`, "seeded at GTID 0-1-14", on the first try: no `lost+found` refusal (#449) |
| Replica | `Slave_IO_Running: Yes`, `Slave_SQL_Running: Yes`, `Using_Gtid: Slave_Pos`, `@@read_only` 1; `Master_SSL_Allowed: Yes`, `Master_SSL_Verify_Server_Cert: Yes`, CA `/etc/mysql/cloudfall/ca.crt` |
| Rows and writes | A row inserted on `h1` was on `h2` within 2 s. `INSERT` as `billing` on `h2`: `ERROR 1290 ... --read-only` |
| Firewall | `h1`'s ruleset held `ip saddr <h2> tcp dport 3306 accept` and no other rule for 3306; 3306 was closed from `h3` and from the controller |
| `ledger` (S-019-21) | `ledger@localhost` has the `ed25519` plugin and logs in with its password |
| Second converge | No replication outcome, no seed; `services.yml` `changed=0` on both hosts (#450, #452) |
| Role swap, one change | `h2` declared primary with `databases` and a `listenAddress`, `h1` its replica; `secrets generate` for the new primary; no baseline converge. First converge: `h1` `demote`, `h2` `wait-promotion`. Second: `h1` `attach`, `h2` `promote` with `lastAppliedGtid 0-1-16`; the seed tasks were skipped |
| After the swap | `h2` writable with no replication, `h1` read-only following `h2` by GTID; a row written on `h2` reached `h1`; both held every row; `h2`'s ruleset admitted 3306 from `h1` only, and `h1` no longer had a 3306 rule (#453) |

## Results: the outside source

Each line held on both 10.11 and 11.4 unless it says otherwise.

| Step | Result |
| --- | --- |
| `binlog_format=STATEMENT` | `service_mariadb_source_unfit`: "its binary log is STATEMENT, not row-based (binlog_format = ROW). Nothing was installed on h4 and nothing on the source changed" (h5 in the 11.4 round); no `mariadb-server` on the replica |
| Binary log off | `service_mariadb_source_unfit`: "it writes no binary log (log_bin is off)"; no `mariadb-server` |
| `RELOAD` revoked | `service_mariadb_source_unfit`: "its account lacks RELOAD ON *.*, which the seed and the link need"; no `mariadb-server` |
| Source unchanged | `@@gtid_binlog_pos` on the source was the same before and after each converge |
| First converge of a fit source | **Failed** at `cloudfall_mariadb : Require the server certificate before the server uses TLS`: "/etc/mysql/cloudfall/ca.crt is missing on h4: remoteAccess of service mariadb-moved serves TLS with the server's certificate from the project CA". No certificate step ran. The volume had been created, and in the 11.4 round `mariadb-server` was installed and running when it stopped |
| Hand step | A key made on the replica, signed by a stand-in CA on the controller, and put at `/etc/mysql/cloudfall/server.{key,crt}` and `ca.crt` |
| Converge again | `seed`, "seeded at GTID 0-3-12" |
| Replica | Read-only, both threads running by GTID, `Replicate_Wild_Ignore_Table: mysql.%`, TLS verified against `/etc/mysql/cloudfall/source-ca.crt`. It held `shop` and `crm` with every row, and no user of the source (only `mariadb.sys`, `mysql`, `root`) |
| After the seed | A row inserted on the source arrived; a user created and granted on the source did not, and the SQL thread kept running with no error |
| The source password | Not found in any `-v` playbook output, any answer, or any project file outside `secrets/`. This path writes no receipt |
| Next converge | No outcome, no seed, `changed=0` |
| Promotion | Source stopped, replica declared `role: primary` with a `listenAddress`: `promote`, `lastAppliedGtid 0-3-15`, "its source refuses connections on 91.99.130.198:3306 from this replica's server". Writable, no replication, `replicate_wild_ignore_table` empty |
| Adoption | `shop` declared with owner `shop` and a `passwordRef`: the converge kept all rows, and `shop` logged in with the declared password, read and wrote `shop`, and was refused on `crm` |

| Criterion | Held | Note |
| --- | --- | --- |
| S-019-12 | **Yes** | First converge seeds; every part of the criterion |
| S-019-13 | **Yes** for a second converge | `changed=0`, no seed. The refusal half (a replica holding data that can't attach) was not run |
| S-019-15 | **Yes** | Two converges, `demote`/`wait-promotion` then `attach`/`promote`, no seed, no row lost |
| S-019-17 | **No**, as built | The replica can't converge without a certificate placed by hand. With it, on 10.11 and 11.4: seeded, followed, a later source user not applied, promoted, and a database adopted with its rows and owner password |
| S-019-18 | **Yes**, on 10.11 and 11.4 | Each refusal stopped before `mariadb-server` was installed; the password appeared nowhere; the source was not changed. "Newer than `version`" was not run |
| S-019-21 | **Yes** | `ed25519` user, password login |

S-019-3, S-019-5 and S-019-11 were not read: the run had no standalone server, no
application database and no `remoteAccess`.

## Findings

- **A replica of an outside source gets no server certificate.** `mariadb_certificate_requirements`
  in `sdk/src/cloudfall/elasticsearch_converge.py` leaves out a replica with `source`; its
  docstring says the role refuses one (#424), which predates #444. `cloudfall requirements show`
  still lists certificate requirement `mariadb` for that server, and the role's `tls.yml`
  requires the file, so the converge stops after creating the volume (and, on the 11.4 round,
  after installing and starting `mariadb-server`). The role's message names `remoteAccess`,
  which this service does not declare
- After the promotion the server needs its certificate as a primary; the hand-made one was kept
  because it was present, so the run did not show the project CA signing it
- The replica of an outside source was stopped by an assertion that runs after the volume and,
  in one round, the package: the stop is not "before any change" for this path

## Caveats

- The source ran in Docker (Ubuntu-based images) rather than from Debian packages
- One run of each case; the source's firewall was open (no rule was needed to admit the replica)
- The pair's role swap was the planned switch; S-019-14 and S-019-16 were not repeated
