### Added

- The logging gateway's port opens on the backend's firewall to each remote collector's addresses only, rendered beside the `remoteAccess` rules and enforced by `logging.yml`; a remote collector with no IP behind a firewall fails `config validate` (`logging_gateway_collector_unaddressed`) (S-011)
