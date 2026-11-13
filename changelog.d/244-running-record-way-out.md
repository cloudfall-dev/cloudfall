### Added

- `cloudfall decisions mark-interrupted <id> --confirm-dead` and `cloudfall operator mark-interrupted <id> --confirm-dead` close a record left `running` by a run that died midway as the new status `interrupted`, once a person confirmed the run is dead; the record keeps its approval and gains `interruption.confirmedBy` (`--by`, default `USER`) and `interruption.confirmedAt`. Nothing runs on the fleet, and neither command is an MCP tool (#244)

### Fixed

- An operator proposal left `running` no longer blocks the operator for good: once closed as `interrupted` it stops blocking new proposals for the same alert or drift, and the refusals of `decisions approve` and `operator approve` name the command that closes it (#244)
- A decision record or proposal receipt write now flushes its directory after the rename, so a power loss cannot bring the old record back (#244)
