### Fixed

- Docs said reading a team's own Ansible inventory was not built: `ARCHITECTURE.md`, `docs/fleet-goals.md` (requirement 14, now Partial) and `docs/brownfield-design.md` (a status per part instead of "nothing is implemented") now say that `inventory show`, `audit`, `observe`, the operations catalog, `why` and `mcp serve --repository` work on a team's repository, and that deploy, rollback, restart and health (#618), backups and the restore drill (#619) and `get host` with each value's source (#620) do not yet (#617)
