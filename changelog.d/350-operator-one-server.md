### Fixed

- An approved operator proposal now runs on the one server it names: every playbook of `converge-services` and `converge-baseline`, the Elasticsearch node preparation included, runs with `--limit <server>`, a drift proposal's verify step inspects that server alone, and the receipt names only the owner passwords set there; before, a staging alert approved by the staging policy converged every declared server, production included (#350)
