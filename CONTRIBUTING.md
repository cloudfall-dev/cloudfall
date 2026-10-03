# Contributing

Thanks for your interest in Cloudfall. The project is young and the fastest way to
help is to try the wedge workflow on a disposable Debian host and report where
it breaks.

## Development environment

Cloudfall requires Python 3.14 and [`uv`](https://docs.astral.sh/uv/). All commands
run through `uv run`.

```console
uv sync
uv run pytest
```

## Checks

Every change must pass the same suite CI runs:

```console
uv run ruff check .
uv run mypy
uv run pytest
uv run cloudfall config validate --project config/examples
uv run ansible-lint engine/ansible
```

## Ground rules

- **Fail fast.** No broad exception handling, no swallowed errors, no
  placeholders. Validation rejects malformed input immediately
- **Typed domain.** Domain concepts use strict value objects, not bare
  primitives; wide unions belong only at system boundaries
- **Module boundaries.** `state/` contains configuration and schemas only; the
  SDK reads and validates state without Ansible internals; the engine consumes
  validated inputs and never silently rewrites state
- **Synthetic fixtures only.** Example state, tests, and docs must never
  contain real hostnames, addresses, provider identifiers, credentials, or
  hardware serials. Playbooks and roles in this repository may reference only
  resources that exist in `config/examples`
- **Evidence over inference.** Status is derived from validated observations
  and receipts; nothing reports success it cannot prove

## Pull requests

Keep changes scoped to one concern. Include tests for behavior changes and
update the relevant README when a workflow changes.

## Releases

After bumping `version` in `pyproject.toml`, record the release's interface
changes, which `cloudfall changelog` serves:

```console
uv run treaty changelog-add cloudfall.app:app
```

It diffs the manifest against the snapshot beside
`sdk/src/cloudfall/schema-changelog.json` and appends one entry; commit both
files with the release.

## Licensing of contributions

Cloudfall is dual-licensed: AGPL-3.0-or-later for everyone, with commercial
licensing exceptions offered by the copyright holder. To keep that model
possible while accepting outside work, contributions carry two grants:

1. Your contribution is licensed under the AGPL-3.0-or-later license of this
   repository
2. You additionally grant the Cloudfall copyright holder a perpetual,
   worldwide, non-exclusive, irrevocable, royalty-free license to use,
   reproduce, modify, sublicense, and distribute your contribution under
   other license terms, including commercial licenses

You keep the copyright to your contribution. By opening a pull request you
confirm that you wrote the contribution or otherwise have the right to submit
it under these terms.
