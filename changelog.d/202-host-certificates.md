### Added

- `cloudfall logging deploy` provides the stack's mTLS material: each host makes its own key and a signing request (`logging-requests.yml`), the project CA signs the missing and due certificates, and they are delivered with the CA; certificates provided by hand are kept (S-011)
