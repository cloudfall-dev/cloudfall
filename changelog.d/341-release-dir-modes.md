### Fixed

- A bootstrap or baseline after a deploy no longer reports changes on each component's `releases/` and `shared/`: the deploy now creates them `0750`, as bootstrap does, instead of flipping them to `0755`, so a converge after a deploy is idempotent; the Nginx worker still reaches a static route's release through its own ACL entry (#341)
