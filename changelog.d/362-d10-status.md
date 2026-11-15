### Fixed

- Docs: D-10 in `docs/decisions.md` read as if every component already ran in the full systemd sandbox; a status line now says the unit sets only `User`, `Group`, `NoNewPrivileges=true` and `PrivateTmp=true`, that secrets reach it through the per-release `EnvironmentFile` (D-21) and deploy tasks run as root, and that the rest of the sandbox is not built yet (#362)
