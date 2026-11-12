### Changed

- The logging backend and collector roles declare Debian 13 (trixie) next to Debian 12, like every other bundled role, so a trixie host built from the bundled Debian 13 baseline can carry the logging stack. Loki, Grafana and Alloy come from Grafana's distribution-independent APT repository; Prometheus comes from the host's Debian release, so a `software.prometheusPackageVersion` pin must name a version that release ships
