## What and why

<!-- What this changes and the problem it solves. Link the issue: Fixes #123 -->

## Checklist

- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest` pass
- [ ] `uv run cloudfall config validate --project config/examples` passes
- [ ] `uv run ansible-lint engine/ansible` passes, if the engine changed
- [ ] A `CHANGELOG.md` entry under `[Unreleased]`, if users will notice
- [ ] Fixtures and docs are synthetic: no real hostnames, addresses or credentials
