### Fixed

- A redeploy copy (`releases/<release>.redeploy-<UTC time>`) whose release command fails is removed with its environment file, as one that fails its gate is, so `rollback <release>` takes the newest copy that passed instead of starting one that never passed its release command or gate; the previous copy and its own environment file stay (#373)
