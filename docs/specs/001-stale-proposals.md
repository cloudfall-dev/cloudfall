# S-001: Stale proposals

status: approved

## Problem

A mutating proposal records the fleet evidence it was made against: `basis.observations`,
the snapshot folder, and `basis.observedAt`, each host's observation time
(`sdk/src/cloudfall/decision.py`, `Basis`). Once a newer `cloudfall observe` replaces
that evidence, the proposal still reads `proposed`, and nothing tells the operator that the
agent decided on a fleet that has since been observed again. D-1 already gave read runs and
repeated proposals their own status (#53); the open proposals that nobody repeated are what
is left of the noise in cloudfall-dev/cloudfall#26.

## Behaviour

A `proposed` decision is **stale** when, for at least one host named in its
`basis.observedAt`, the observation file for that host in the folder its
`basis.observations` names now holds a later `spec.observedAt` than the basis records.
Hosts the basis doesn't name don't count, a host whose observation file is gone doesn't
count, and a proposal whose basis names no host is never stale. Only `proposed` records can
be stale; every other status is final for this purpose.

Staleness is worked out when the record is read, never stored: the record file doesn't
change, `config/schemas/v1/operation-decision.schema.json` gains no status value, and
`DecisionStatus` is unchanged. A record written by this version loads in an older
`cloudfall`.

- `decisions list` and `cloudfall why` add `"stale": true` or `"stale": false` to each
  `proposed` decision in their output, and a `staleHosts` list naming the hosts whose
  observation is newer when it is true. Decisions with any other status carry neither key
- `decisions list --stale` and `cloudfall why --stale` keep only the stale decisions. Given
  with `--status`, both filters apply
- `decisions show` reports `stale` and `staleHosts` the same way for a `proposed` record
- `decisions approve` on a stale proposal still runs: its attestation prompt names the
  stale hosts and their newer observation time before asking for the decision id
- A basis observation folder or file that can't be parsed stops the command with
  `RECORD_INVALID`, like a broken decision record

Touches `sdk/src/cloudfall/decision.py`, `sdk/src/cloudfall/app.py`, `decisions list`,
`decisions show`, `decisions approve`, `cloudfall why`, and the MCP tools generated from
them.

## Acceptance criteria

- S-001-1: A `proposed` decision whose basis names host `hz1` observed at T1 reads `"stale": true` with `"staleHosts": ["hz1"]` in `decisions list` once `hz1`'s observation file in the basis folder says `observedAt` T2 later than T1
- S-001-2: The same decision reads `"stale": false` and no `staleHosts` when the observation file still says T1, when it is missing, or when the basis names no host
- S-001-3: A decision with any status other than `proposed` has no `stale` or `staleHosts` key in `decisions list`, `decisions show`, or `cloudfall why` output, whatever the observations say
- S-001-4: `decisions list --stale` returns only the stale decisions, and `decisions list --stale --status proposed` returns the same set
- S-001-5: `cloudfall why --stale` returns only the stale decisions and reports `stale` and `staleHosts` as `decisions list` does
- S-001-6: `decisions show <id>` reports `stale` and `staleHosts` for a stale `proposed` decision
- S-001-7: Working out staleness writes nothing: the decision record file's bytes are unchanged after `decisions list`, `decisions show`, and `cloudfall why`
- S-001-8: `decisions approve` on a stale proposal names each stale host and its newer `observedAt` in the attestation prompt and, once the id is typed, runs as it does for a fresh proposal
- S-001-9: An observation file in the basis folder that isn't valid JSON makes `decisions list` exit `RECORD_INVALID` with an error naming the file

## Out of scope

- A stored `stale` status: it would need a write on every read or a sweep command, and a new enum value that an older `cloudfall` refuses to load. Revisit if records leave the repository (an API or a dashboard that can't read the observations)
- Refusing to approve a stale proposal: with observations collected on a timer (the M10 pattern), every proposal goes stale within minutes and approve would refuse them all. The prompt names the staleness instead
- Staleness by repository commit: the basis records no commit today, so there is nothing to compare. A later spec can add one to the basis
- Hosts observed now that the basis didn't name: whether a new host makes a fleet-wide proposal stale depends on its target pattern, which the record doesn't resolve to hosts

## Decisions relied on

- D-1

## Issues

## Verification
