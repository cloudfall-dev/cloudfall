### Fixed

- `deploy`, `rollback` and `restart` contact only the component's own servers (its `component_<id>` inventory group, still in inventory order), and a backup contacts only the service's server, so an unreachable server the component or service is not on no longer fails them; a component with no servers in the inventory, or a service on no inventory server, now fails before any host is contacted (#220)
