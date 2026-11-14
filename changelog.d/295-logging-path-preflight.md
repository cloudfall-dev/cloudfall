### Fixed

- `logging deploy` checks, before anything is installed, that each service user can reach the files it reads (Grafana's admin password, Alertmanager's SMTP password and Telegram token files, Alloy's client TLS files) through every existing parent directory, and stops naming the directory that blocks it, as in `/etc/cloudfall is 0700 root:root; grafana can't reach /etc/cloudfall/logging/grafana-admin-password`, where Grafana used to fail only when it started; the host's directory modes are left as they are (#295)
