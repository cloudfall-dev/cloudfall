### Fixed

- `cloudfall changelog` no longer lists the whole interface as added in every release: the release flow now puts back the manifest snapshot and `schema-changelog.json` from the previous release's tag before `treaty changelog-add` runs, so an entry lists only what changed since that release and a release whose interface did not change adds no entry. Release commits are not on `main`, so each release had started with no snapshot (#457)
