### Added

- `deploy.yml`, `rollback.yml` and `restart.yml` hold the component's lock on each server, at `/var/lib/cloudfall/locks/<component>.json`, from before they change anything there until that server's turn ends, passed or failed, so runs from two controllers, `cloudfall-engine playbook run` and approved runs of those playbooks no longer race on `current`, the unit file or the release command's unit: a second run fails on that server, naming the action, user, controller, start time and age of the lock it found (#221)

### Changed

- Contract: `cloudfall lock clear <component>` now also reads the component's lock on each of its servers through the new `lock.yml`, shows them with their ages in the same prompt, and once the component id is typed removes exactly the locks it showed; it takes `--engine` and `--inventory-file`, answers `serverLocks` and `unreadServers` (a server it could not reach), may exit `ENGINE_STEP_FAILED`, and is listed among the commands that change servers (#221)
