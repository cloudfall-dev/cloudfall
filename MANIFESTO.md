# The Cloudfall Manifesto

Cloudfall exists because letting an AI agent run your servers should not
require trusting its word, and leaving a PaaS should not require a platform
team. Ansible is the hand, the agent is the brain, Cloudfall is the
conscience: it says what the agent may do, checks every result, and keeps
the record. These are the principles the project is built on. They are
constraints we accept, not aspirations we advertise.

## 1 · You should own your infrastructure

A steady-traffic SaaS pays a PaaS several times the price of the hardware it
runs on, forever, in exchange for not having to think about servers. By the
month the bill crosses $1,000 you are funding a platform team you never
hired. That trade made sense when the alternative was Kubernetes or
hand-rolled SSH scripts. It stops making sense the moment the operational
feel of a PaaS — health-gated deploys, monitoring, databases that provably
restore — can live on one or two servers you own. The bill should buy
computers, not margins.

## 2 · Evidence over inference

Status is derived from validated observations, never from the fact that a
command exited zero or a playbook exists. Every operation has a verify
step: not "the playbook ran" but "the service is healthy on every host it
touched". Audits compare the declared state with what a read-only
inspection actually found, and report drift with distinct exit codes.
Cloudfall never reports success it cannot prove; when it does not know, it
says `unknown` instead of guessing.

## 3 · The record answers why

AI agents will operate infrastructure. The question is on what terms. An
agent's own log says a tool was called; that is not a record. Cloudfall
writes an audit entry for every decision: what the fleet looked like, what
the agent proposed, the diff it previewed, who approved, and what verify
reported. The agent acts only through declared operations, each with a risk
level it cannot change, and every server-changing action waits behind a
gate the agent does not control. Autonomy is earned from that record, per
operation class, never switched on globally. The same evidence rules bind
the agent that bind a human: it cannot claim an outcome the receipts do not
show, and it cannot do something it cannot later explain.

## 4 · One config, and it is yours

There is one description of your fleet and you own it: plain files in your
repository, versionable, diffable, readable by humans and agents alike.
Today that is typed YAML validated against JSON Schemas; where a team
already runs Ansible, it will be their own inventory with one `cloudfall`
key added, never a second copy that drifts. No execution logic hides in
it, no engine silently rewrites it, and no secret values ever live in it.
If a fact about your platform matters, it is either declared in the config
or observed as evidence; there is no third place.

## 5 · Boring on purpose

Applications run as native systemd services with artifact releases and
symlink rollback, behind nginx, next to loopback PostgreSQL, on stock
Debian. No Kubernetes, no container orchestrator, no bespoke daemon that
only we understand. The exit test: any Linux admin can take over the server
without Cloudfall installed. A tool that promises to free you from one
platform must not become the next one.

## 6 · Never guess silently

The Render importer emits a report of everything it did not import, every
assumption it made, and every step still required before cutover. The
migrate plan pauses at DNS verification rather than pretending a cutover is
safe to automate blindly. Validation fails fast on malformed input instead
of repairing it. Wherever Cloudfall must choose between convenient and
explicit, it chooses explicit.

## 7 · Proven live or labeled unproven

Every implemented layer is exercised on disposable servers against real
targets: real certificates, real DNS flips on owned domains, real rollbacks
of deliberately bad releases. What has not been proven live is listed as
such in the README and the roadmap. "Proven live or labeled unproven" is
not marketing copy; it is the release policy.

## 8 · Open core, never crippled

Cloudfall is AGPL-3.0. The free tool must be genuinely sufficient to leave
a PaaS and run your own server; sustainability never comes from holding
back the open core. What is free stays free.

---

If Cloudfall ever reports a deploy it cannot prove, cannot say why the
agent did something, hides a gap it knows about, or requires Cloudfall
itself to keep your server running, that is a bug against this document.
File it.
