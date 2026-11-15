### Fixed

- A deploy no longer fails on one slow download of the pinned uv release: the deploy role retries the download five times, ten seconds apart, still checking its SHA-256, before failing, and a test over every role now requires each `get_url` download to retry a bounded number of times (#372)
