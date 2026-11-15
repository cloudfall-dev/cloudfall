### Added

- A Component's optional `deployment.retainReleases` (an integer, at least 2, 5 when absent) says how many releases a deploy keeps on each server; the SDK inventory and the deploy role carry it, and `retainUntilCleanup: true` still validates and means the cleanup is on (#224)
- The release receipt names, in each server entry whose gate passed, the releases the deploy removed there under `removedReleases`; `release-receipt.schema.json` accepts it and refuses it on a failed entry (#224)

### Fixed

- Old releases are removed: once a deploy passes its health gate on a server, it keeps `deployment.retainReleases` releases under `releases/` and removes the older ones, a release's redeploy copies with it, always keeping the deployed release, `current` and the release before it whatever their order; a failed gate removes nothing, check mode only reports what would go, and a directory that is not a release id is left alone (#224)
- A rollback to a release that is no longer on the server says it may have been removed by a deploy's cleanup and names the releases still kept there (#224)
