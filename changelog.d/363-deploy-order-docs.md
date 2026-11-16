### Fixed

- Docs gave the deploy order as health-check gate before symlink switch: `ARCHITECTURE.md` now says the switch and the restart come first, so the gate runs on the live release, and `ARCHITECTURE.md`, `docs/ci-deploy-guide.md` and the Render and Neon migration guides say a rollback, automatic or `cloudfall rollback`, does not undo the release command, so a migration must stay compatible with the release before it (#363)
