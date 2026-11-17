### Fixed

- A granted MCP run whose agent record the proposal store can't save now exits `PRECONDITION` before any engine step, naming the store folder (`agent_record_unwritable`) or, for an identical call started in the same second, the existing record (`agent_record_exists`), instead of an internal error; a record that can't be settled after the run started no longer masks the run's own error (#489)
