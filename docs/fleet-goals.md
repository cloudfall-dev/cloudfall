# Fleet goals: 10–100 applications on shared nodes

Status: adopted goal statement. This document defines what fleet operation
must deliver and is the reference the roadmap sequences against. The
[architecture](../ARCHITECTURE.md) describes the durable design;
[`availability-design.md`](availability-design.md) and
[`hybrid-storage-design.md`](hybrid-storage-design.md) specify two of the
mechanisms referenced here, and the
[brownfield design](brownfield-design.md) specifies the record and the
operations catalog for requirements 12–14.

## Goal

Host 10–100 applications on the shared resources of a small fleet of bare
metal or VPS nodes, at a fixed and predictable cost per node, operated from
one management host by an AI agent without Kubernetes, on a config the
team owns, with a record that answers why for every change on every node.
The fleet may be one Cloudfall set up or one the team already runs with
Ansible; the requirements are the same.

## Requirements

Economics:

1. **Fixed cost per atomic node.** The node (a Hetzner-class dedicated
   server or VPS) is the unit of purchase and capacity; adding capacity
   means adding nodes, never renegotiating a platform bill
2. **Fixed resources, enforced sharing.** A node's resources are shared by
   its applications under declared, enforced limits. Sharing without
   enforcement is a hope: one leaking application must not take down its
   neighbors (see requirement 8)
3. **Capacity grows by adding nodes.** Expansion is horizontal and
   incremental; no resource on the fleet requires replacing existing nodes
   to grow

Placement:

4. **Applications and components are movable.** Rebalancing an application
   between nodes is a config change followed by convergence — including its
   data (requirement 9) and its routing (requirement 10), or the property
   is only true for stateless components

Operations:

5. **Fast recovery and fast expansion.** A failed unit is detected,
   diagnosed, and remediated in minutes through the alert-and-operator
   loop; a new node reaches production readiness in one command; a lost
   node is rebuilt from config and backups, not from memory
6. **Failover on demand, automatic promotion where declared.** Failover is
   a per-service opt-in through the `availability` block, priced per
   service — a formation for the databases whose revenue justifies a
   standby, fast recovery plus point-in-time restore for everyone else.
   Fleet-wide formations would multiply the host count and destroy
   requirement 1
7. **One-command node setup from the management host.** An operator adds a
   server to the config and runs one command; baseline, hardening,
   firewall, observability, and service convergence follow without manual
   steps on the node

Added by the shared-density goal — absent any of these, 10–100 applications
on shared nodes fails in practice:

8. **Per-component resource declarations, enforced by systemd.** Every
   component declares its memory, CPU, task, and I/O envelope; the deploy
   role renders them into the unit (`MemoryMax`, `CPUQuota`, `TasksMax`,
   `IOWeight`) and the audit proves them. Declared reservations against
   node capacity are also the fleet's capacity accounting: what fits where
   is computed from config, not guessed
9. **Data movement as a first-class verb.** Moving an application moves its
   state: databases (the existing verified dump-and-restore path) and file
   storage, with receipts. Rebalancing granularity is the application with
   its data, not the process alone
10. **Routing follows placement.** Domain routes re-render and reconverge
    as a consequence of moving a component; the proxy layer is never edited
    to match placement by hand
11. **Node drain and decommission.** The reverse of requirement 7:
    evacuate every component, dataset, and route from a node by config
    change, prove the fleet healthy without it, then retire it. Planned
    drain of an aging node and recovery from a failed one are distinct,
    both routine

Added by the agent-operated goal — an agent at this density is only
acceptable if the following hold:

12. **Every operation is declared, with a risk level and a verify step.**
    The agent's tool list is a catalog of operations the team registered,
    each with typed inputs, a risk level it cannot change, preconditions,
    and a verify step that reports per host. A run without a verify result
    is not an outcome. Anything not in the catalog is not available to the
    agent
13. **Every decision leaves an audit entry.** One record per decision
    holding the evidence snapshot it was based on, the proposed diff, who
    approved (human, policy, or client handshake) and the verify result.
    "Why did the agent do that" is answered from the record for any node,
    operation or time window, and autonomy per operation class is granted
    on that record, never globally
14. **One config, and it is the team's.** The fleet is described once, in
    plain files the team owns; where the team already runs Ansible, that
    is their inventory with one `cloudfall` key, not an import and not a
    second copy. Cloudfall never rewrites a file it does not own the key
    in, and never keeps state the team cannot delete by removing the key

## Current status against the requirements

| # | Requirement | Mechanism | Status |
| --- | --- | --- | --- |
| 1 | Fixed node cost | Node as purchase unit, `ServerType` | In place |
| 2 | Enforced sharing | Requirement 8 | Missing |
| 3 | Horizontal growth | All-fit-all model, movable components | Designed, proven at one node |
| 4 | Movable applications | Multi-server assignment + convergence | Partial: components move; data and routing move manually |
| 5 | Fast recovery / expansion | M7 alerting, M8/M9 operator, `server:baseline` | Proven live for recovery; expansion proven for VPS, bare-metal storage unexercised |
| 6 | Failover on demand | `availability` block, formation mode | Designed, not implemented (demand-gated) |
| 7 | One-command node setup | Baseline over SSH from config | In place for installed Debian; provisioning (installimage, RAID) out of band |
| 8 | Resource limits | systemd resource control from declared envelopes | Missing |
| 9 | Data movement verb | `data migrate` + file-storage move + receipts | Partial: databases yes, file storage no |
| 10 | Routing follows placement | Domain re-render on convergence | Partial: rendering exists, not wired to placement change |
| 11 | Drain / decommission | Evacuation procedure over 9 + 10 | Missing |
| 12 | Declared operations with verify | `Operation` catalog, MCP annotations, verify playbook | Partial: MCP tools are annotated, server changes preview until confirmed, and approval stays a person's command; deploys, backups and migrations verify; no catalog over arbitrary playbooks, no declared verify step |
| 13 | Audit entry per decision | Receipts as the record, `cloudfall why` | Partial: operator proposals and every deploy, backup and drill write receipts; no single entry joining snapshot, diff, approver and verify; no query |
| 14 | One config, the team's | Fleet reader over their inventory, `cloudfall` key | Missing: the config is Cloudfall's own resources today (design written) |

## Sequencing implications

In value order for the roadmap, after M10 (proven restores and real
alerting — the floor everything above stands on):

1. Requirements 12 and 13, the operations catalog and the audit entry
   (roadmap M15) — the product in its smallest form; the record only
   compounds on fleets that exist, so it comes before anything that adds
   density
2. Requirement 14, the team's Ansible as the config (M16) — the entry
   point for every fleet that was not set up by Cloudfall, which is most
   of them
3. Requirement 8, resource declarations and enforcement (M11) — the first
   thing that fails at density, and the cheapest of the remaining items
4. Requirements 9 and 10, then 11 (M12) — data movement and routing make
   requirement 4 true, which is what "ready to scale" means here, and
   drain is needed the first time a node ages out; any real fleet's aging
   hardware guarantees that day comes
5. PostgreSQL point-in-time recovery, the availability schema, and the
   expansion-ready-at-one-node prerequisites (M13) — the schema and
   prerequisites land here because they are cheap and make the formation
   purchasable later
6. Requirement 6 implementation, the formation (M14) — demand-gated:
   built when a service's revenue justifies a standby, not before
7. Safe edits and the hosted record (M17), and bare-metal provisioning
   closing the last gap in requirement 7 — each when a workload needs it

## Non-goals

- Kubernetes, container orchestration, or automatic scheduling: placement
  is declared, with capacity accounting to inform it, not decided by a
  scheduler
- Fleet-wide high availability by default: availability is bought per
  service where it pays for itself
- Multi-region or multi-provider topology: the fleet is single-site until
  the architecture says otherwise
- Autoscaling: capacity changes are deliberate config changes by the
  operator
- Being the brain: the agent decides what to run; Cloudfall declares what
  it may run, gates it, verifies it and records it. It proposes nothing on
  its own beyond the operator's alert-driven remediations
