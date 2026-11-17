# Proving run: PostgreSQL `remoteAccess` over TLS (S-006-9)

| | |
| --- | --- |
| **Date** | 2026-11-17, 09:57 to 10:05 UTC |
| **Scope** | #145: S-006-9, the three `psql` attempts against a PostgreSQL service converged with `remoteAccess` |
| **Hosts** | Three Hetzner Cloud cx23 (nbg1; fsn1 had no cx23 capacity), stock Debian 13, on one private network `10.0.0.0/24`: `pg1` (10.0.0.2) with the service, `cli2` (10.0.0.3) in `allowedSources`, `out3` (10.0.0.4) outside it. All deleted after the run |
| **Code** | `main` at 45a0614; PostgreSQL 17 from Debian, `psql` 17.12 on the clients |
| **Verdict** | S-006-9 held: an allowed address connects over TLS, a plain connection from it is refused by `pg_hba.conf`, and an address outside `allowedSources` gets no connection |

## Method

A project with one server (`pg1`), a baseline with a default-deny firewall that opens
nothing, and `postgresql-main`: database `billing` with owner `billing` and a `passwordRef`,
and `remoteAccess` with `listenAddress: 10.0.0.2` (the private address) and
`allowedSources: [10.0.0.3/32]`. The password was stored sops-encrypted with a throwaway age
key. `cloudfall secrets generate` answered `noop`: an owner password is not a requirement it
generates, so the fragment was written with `sops` by hand.

Entry points: `cloudfall config validate`, `cloudfall baseline converge --yes`, and
`converge_services`, the function behind the `converge_services` MCP tool, called from a short
script that also kept the playbook output. `postgresql-client` was installed on `cli2` and
`out3` by hand, with the password in `~/.pgpass`.

## Results

| Step | Result |
| --- | --- |
| Baseline, then services | Both passed; the services recap was `ok=54 changed=23` |
| What converged | `listen_addresses = '127.0.0.1,10.0.0.2'`, `ssl = on` with the snakeoil pair; listeners on `127.0.0.1:5432` and `10.0.0.2:5432` only; `pg_hba.conf` held exactly `hostssl billing billing 10.0.0.3/32 scram-sha-256` between the Cloudfall markers; nftables held `ip saddr 10.0.0.3 tcp dport 5432 accept` and no other rule for 5432 |
| `cli2`, `sslmode=require` | `select 1` returned `1`; `pg_stat_ssl` showed TLSv1.3 |
| `cli2`, `sslmode=disable` | `FATAL:  no pg_hba.conf entry for host "10.0.0.3", user "billing", database "billing", no encryption` |
| `out3`, `sslmode=require` | `timeout expired` after the 10 s `connect_timeout`: the firewall dropped it |
| Public address | Port 5432 on `pg1`'s public IPv4 was closed from the controller |

| Criterion | Held |
| --- | --- |
| S-006-9 | **Yes**, all three attempts as the criterion states |

## Findings

- `cloudfall secrets generate` does not create a PostgreSQL owner's password, and
  `cloudfall secrets input` takes only a requirement's name, so the operator writes the
  `passwordRef` fragment with `sops` by hand. The spec does not promise otherwise; the service
  guide could say it

## Caveats

- One run, one Debian release, a private network rather than public addresses
- `audit` was not run (#143's false drift on 5432 was noted in the hand-off)
