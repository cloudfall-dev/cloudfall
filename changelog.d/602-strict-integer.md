### Breaking

- `cloudfall config validate` now refuses a whole-number float such as `8100.0` in any integer field, naming the field's path like any other schema error; before, it passed, and the command that read the value (`cloudfall domains apply` for a Domain's `spec.proxy.upstream.port`) crashed with `HANDLER_CRASHED`. Before upgrading, write those values as plain integers (`8100`, not `8100.0`); `cloudfall config validate` on the new release names each one (#602)
