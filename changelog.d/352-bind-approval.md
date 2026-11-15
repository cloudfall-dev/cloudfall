### Fixed

- `decisions approve` runs only what was checked: a proposal's record now binds the sha256 of its playbook and verify playbook and, when the project is in a git work tree, its HEAD commit and a digest of its uncommitted changes (leaving out the decisions directory, `record/` and `tmp/`), and approve recomputes them and the diff file's sha256 before anything runs; any difference is refused with `PRECONDITION` and `decision_binding_changed`, naming what changed and asking for a new proposal, and the record stays `proposed`. Before, an edited playbook, role or diff file ran unseen under the old diff's approval. Outside git only the two playbooks and the diff are bound (#352)

### Changed

- Open proposals made before the upgrade carry no binding, so `decisions approve` refuses them with `decision_binding_changed`: propose them again (#352)
