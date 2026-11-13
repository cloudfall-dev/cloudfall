### Fixed

- A redeploy copy (`releases/<release>.redeploy-<UTC time>`) that fails its health gate is removed by the deploy's rollback once the previous copy runs again, so `rollback <release>` takes the newest copy that passed instead of switching to the failed one; the copy's time now has microseconds, so two redeploys in the same second get two directories (#245)
