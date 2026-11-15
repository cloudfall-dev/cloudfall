### Fixed

- `decisions approve` no longer settles a decision `verified` when its verify step observed no host: every host the check run or the approved run left recap counts for must have counts from the run and from the verify step too, and a verify play that matched no host or skipped a targeted one settles the decision `failed`, with a verdict naming the hosts that were not observed. Before, a verify step that exited 0 and listed no host as changed proved the run, even when it looked at nothing (#360)
