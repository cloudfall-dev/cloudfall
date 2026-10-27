### Fixed

- `domains.yml` now fails before it changes any server when a Domain it would render has no `proxy.upstream`, and names every such Domain; before, the site role's assert stopped the run only on that Domain's server, after `serial: 1` had already converged the servers before it. `upstream` stays optional in the schema, for a Domain the fleet routes with its own proxy (#90)
