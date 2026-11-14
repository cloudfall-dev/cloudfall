### Added

- `audit` checks a logging backend's dashboards (`logging.dashboards`): `inspect` records the name and sha256 of each file in Grafana's Cloudfall dashboards directory (new `loggingDashboards` evidence, never anything else of Grafana's), and the check is compliant when they are exactly the dashboards the installed release bundles; drift names each missing, extra or changed file (S-016)
