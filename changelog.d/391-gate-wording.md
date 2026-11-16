### Fixed

- Docs promised a gate the agent does not control for every server-changing action: `ARCHITECTURE.md`, `docs/brownfield-design.md` and `MANIFESTO.md` now say it holds for declared operations (a person approves at a terminal) and the operator (a person at a terminal or an `OperatorPolicy` grant), while in project mode the `cloudfall mcp serve` confirmation (`yes: true`, `confirm_destructive: true`) is the agent's own, except for a release whose application declares `approval: manual`; policy-gated self-confirmation is planned (#391)
