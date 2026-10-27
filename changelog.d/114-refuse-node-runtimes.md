### Changed

- **Breaking:** `cloudfall config validate` now refuses a Component with `runtime.type` `node` or `pm2`, or `runtime.packageManager` `npm`, `pnpm` or `yarn`, with `CONFIG_INVALID` and code `component_runtime_unsupported` naming the field: these configs validated before but could never deploy, since the deploy role runs only Python with uv. The schema keeps those values as reserved. The Render importer no longer writes a Component for a Node service; it lists the service as an unsupported gap instead (#114)
