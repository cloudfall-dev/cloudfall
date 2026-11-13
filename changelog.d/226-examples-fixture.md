### Fixed

- The test suite passes on a checkout whose `config/examples/tmp/` holds rendered inventory or observations (as `task inventory` and CI leave it): every test that copies the examples now goes through one helper, `sdk/tests/examples_project.py`, that leaves that folder behind, and a test refuses a direct copy (#226)
