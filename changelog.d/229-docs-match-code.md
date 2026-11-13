### Fixed

- Docs that no longer matched the code: `ARCHITECTURE.md` now says a project pins `cloudfall==<version>`, that decision records are committed while runtime state stays under `tmp/`, and which `Taskfile.yml` tasks call Ansible directly; the `AGENTS.md` that `cloudfall init` writes names the `cloudfall==<version>` pin instead of a `rev` pin; `CONTRIBUTING.md` names `config/` instead of a `state/` directory; the CLI evaluation under `evaluations/cloudfall/` uses repository-relative paths (#229)
