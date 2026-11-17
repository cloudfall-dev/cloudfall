### Fixed

- When a granted or person-approved MCP run's agent record can't be saved after the run started, the error the agent receives now keeps the run's own exit code and message and adds `context.record_lost`, naming the store folder, the record, the status it was last saved with, and the store error; before, the store error went into an exception note treaty drops, so the agent never learned the record was left `running`. A run that crashed meanwhile keeps exit 1 and `HANDLER_CRASHED`, now with that context and the exception's name, and no longer prints a traceback to stderr (#496)
