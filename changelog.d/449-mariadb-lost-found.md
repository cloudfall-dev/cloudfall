### Fixed

- A new MariaDB replica on an ext4 data volume is seeded on its first converge instead of refused with `service_mariadb_replica_unattachable ... holds 1 database(s)`: the role no longer counts or lists a `#mysql50#` name, which is how MariaDB shows a directory that is no database (the volume's `lost+found`) until the managed settings ignore it. A replica's backup and restore drill leave it out the same way, so they never try to dump or check it (#449)
