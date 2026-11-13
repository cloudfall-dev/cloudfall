### Fixed

- An operator proposal closed as `interrupted` now suspends autonomy for its operation kind the way a `failed` one does, until a human-approved run verifies, and an autonomous run closed as `interrupted` counts toward `maxAutonomousPerHour` from its `interruption.confirmedAt`; before, a killed unattended run left the next proposal for the same alert free to run unattended while what the first run changed was unknown (#258)
