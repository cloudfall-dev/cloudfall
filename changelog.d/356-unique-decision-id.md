### Fixed

- Two proposals of one operation in the same second no longer share a decision id: the id is reserved before the check run writes anything, and a second proposal in that second takes `<operation>-<stamp>-2`, then `-3`, so each record keeps its own diff and host stats, instead of the second overwriting the first one's diff and then failing with `decision_exists`; a proposal whose check run fails frees its id and leaves no record (#356)
