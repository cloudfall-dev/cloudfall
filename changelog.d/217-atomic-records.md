### Fixed

- Decision records and operator proposal receipts are written through a temporary file and renamed into place, so a crash or a full disk mid-write no longer leaves a truncated record; a new record is created exclusively, so two writers of one id can no longer both succeed, and each read-modify-write of a record holds its directory's lock (#217)
- An approval is recorded, with the new status `running`, before the first playbook or the operator's executor starts. A run killed midway stays `running` and is refused by `decisions approve` and `operator approve` with `PRECONDITION` (`decision_run_interrupted`, `operator_proposal_run_interrupted`) instead of being approvable and run a second time; `running` is added to both record schemas (#217)
