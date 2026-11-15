### Changed

- `decisions approve` gives the approved run and its verify step their own time limit of 6 hours (21600 seconds) instead of the check run's 1800 seconds, which check mode keeps. A run that reaches it is stopped with its whole process group and the record settles `failed` with the verdict `the run timed out after 21600s and was stopped`; the run's output now goes to its log as it comes, so a stopped run keeps what it printed. Before, a run past 1800 seconds was killed, its record stayed `running` with no outcome, and the CLI's log of it was lost (#359)
