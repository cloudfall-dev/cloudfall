### Added

- `cloudfall changelog` lists entries: each release now records its interface changes (commands, flags, output fields, exit codes) in `schema-changelog.json` and its manifest snapshot, both in the wheel, as the release bot's `after_stamp` step runs `treaty changelog-add cloudfall.app:app`. The first release seeds the snapshot, so its entry lists the whole interface as added; a release whose interface did not change adds no entry. The Releases section of `CONTRIBUTING.md` names no manual step any more (#378)
