### Added

- Certificate provider, controller side: a project CA per environment, kept sops-encrypted under `secrets/<environment>/pki/`, that signs host requests for exactly the names and usage a requirement grants (S-011)
