# Cloudfall

## Mission

Cloudfall is the operator's record for self-hosted servers run by an AI agent: it says what
the agent may do, verifies every result, and keeps the evidence, so leaving a PaaS needs no
platform team.

## Users

- U-1: Teams leaving a PaaS (Render, Heroku) for servers they own
- U-2: Operators who let an AI agent (Claude Code, Codex, any MCP client) run their fleet
- U-3: Teams already running a fleet with Ansible who want a gate and a record over it

## Goals

- G-1: Host 10 to 100 applications on a small fleet of owned nodes at a fixed cost per node, operated from one management host by an agent
- G-2: Every operation has a verify step and every decision an audit entry; cloudfall why answers from the record alone
- G-3: A PaaS app moves over with the operational feel kept: health-gated deploys, rollback, monitoring, databases that provably restore
- G-4: Works on the team's own Ansible inventory and config, not only on one Cloudfall set up

## Non-goals

- N-1: Kubernetes, container orchestration, or automatic scheduling
- N-2: Fleet-wide high availability by default
- N-3: Multi-region or multi-provider topology
- N-4: Autoscaling
- N-5: Being the brain: the agent decides; Cloudfall gates, verifies, and records

## Quality bar

- Q-1: Never report success it cannot prove; say unknown instead of guessing
- Q-2: Every server-changing action previews and waits behind a gate the agent does not control
- Q-3: Configs that validate today keep validating and rendering the same, or the change is a breaking one with a changelog entry
- Q-4: Plain Debian and Ansible; secrets never land in the record
