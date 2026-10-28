### Fixed

- A first deploy over a hand-made systemd unit of the same name no longer loses it when the release fails its health gate: the deploy role keeps a unit without the `# Managed by Cloudfall` header at `/etc/systemd/system/<name>.service.pre-cloudfall` before installing the managed unit, and on a failed first release copies it back, restores its enabled state and starts it again when it was active; check mode reports the unit it would keep, and a hand-made unit that differs from an earlier kept copy refuses the deploy (#151)
