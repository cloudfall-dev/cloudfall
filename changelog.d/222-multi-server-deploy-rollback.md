### Fixed

- A component on several servers no longer ends a failed deploy on two releases: when a later server fails its health gate, the servers that switched to the release earlier in the run go back to the release and service unit they had (a first release is stopped), last one first, each gated again, and the failure names them (#222)
- The release receipt `<receipts>/<component>.json` names every server of the deploy instead of only the last one, which overwrote the others (#222)
- A failed deploy whose component has a release command says the command already ran and is not reverted; the engine README says a release command must work with the previous release too (#222)

### Changed

- The release receipt holds one entry per server under `spec.servers` (the release the server ended on, `previousRelease`, `deployedAt`, `healthCheckPassed`, `rolledBack` and the command `healthCheck`), with the deployed `release`, `artifactSha256` and `releaseCommand` once per receipt; `release-receipt.schema.json` accepts it and still accepts a receipt written before, with one `server`, which reads as a one-entry list (#222)
