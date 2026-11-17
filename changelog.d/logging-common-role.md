### Changed

- The logging backend and collector roles share one copy of the tasks they both ran: the delivery of provided files, the reach preflight, the package pin check and the package holds, with the `reach` filter, now live in the `cloudfall_logging_common` role, which both include by task file. What a logging deploy does on a host is unchanged
