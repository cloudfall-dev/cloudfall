# Security policy

Cloudfall manages servers over SSH and renders infrastructure configuration, so
security reports are taken seriously.

## Reporting a vulnerability

If you are not a maintainer, use GitHub's private vulnerability reporting on
this repository ("Report a vulnerability" under the Security tab). Do not open
a public issue for security-sensitive findings.

Please include the affected component (`config/`, `sdk/`, `engine/`), a
reproduction, and the impact you believe it has.

## Findings by the maintainers

The maintainers, and the agents that work for them, review the code that is
already public and track much of what they find in public issues. The rule
they follow:

- A finding that needs access the attacker would already have (a shell on a
  managed host, write access to the project directory, the operator's own
  credentials) is tracked as a public issue and fixed in the open
- A finding that a stranger could exploit remotely, without such access, stays
  private until a fix is released, for the maintainers too: it goes through a
  private security advisory on this repository, not a public issue

## Scope notes

- State directories must never contain secret values; schemas and validation
  are designed to reject them. A way to smuggle credentials into rendered
  inventory or observations is a vulnerability
- The inspection pipeline is read-only by design. Any code path through which
  `cloudfall_inspect` or the audit toolchain modifies a target host is a
  vulnerability
- Rendered configuration that weakens documented trust boundaries (proxy
  header trust, mTLS requirements, loopback-only listeners) is a vulnerability
