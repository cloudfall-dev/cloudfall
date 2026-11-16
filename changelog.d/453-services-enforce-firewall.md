### Fixed

- `cloudfall services converge` enforces each host's declared firewall, the ruleset `cloudfall baseline converge` applies with the services' port rules in it, before it converges that host's services, so after a MariaDB role change the new primary admits its new replica in the same converge instead of timing out until a baseline converge ran. A host whose baseline declares no firewall is left alone (#453)
