### Fixed

- A MariaDB replica declared `primary` whose link has not connected (its IO thread still `Connecting`, so it reports `Master_Server_Id: 0`) is no longer promoted over a running, writable primary: the role finds its source by the link's `Master_Host` and `Master_Port` too, matched against the hostname, addresses and port the inventory now renders for each server of the pair, reads it, and stops with `PRECONDITION` while it is writable. A link to a source that is no declared service and is not running is promoted only when the controller can't reach the source's port either (#440)
