### Fixed

- `cloudfall config validate` now refuses a whole-number float such as `8100.0` in any integer field, naming the field's path like any other schema error; before, it passed, and the command that read the value (`cloudfall domains apply` for a Domain's `spec.proxy.upstream.port`) crashed with `HANDLER_CRASHED`. A project that writes an integer as a float must write it as a plain integer (#602)
