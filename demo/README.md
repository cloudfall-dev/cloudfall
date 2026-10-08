# Cloudfall live demo

The hosted demo at demo.cloudfall.dev: a visitor picks one of four NVIDIA
Nemotron models and runs a real incident. The model is live, on Nebius
Token Factory, through `cloudfall agent investigate`; the host is played
back from the disk-full incident recorded on a Hetzner cx23
(`--replay`), so nothing a visitor does can touch a server. Approving a
proposed fix plays back the approval recorded on the same host, its verify
step and the disk going from 100% to 5%; approving the destructive one is
refused.

## Layout

```
src/cloudfall_demo/server.py      Starlette app: the page, /api/config, /api/runs (NDJSON stream), /api/approve
src/cloudfall_demo/static/        the page, and the evaluation it shows
incident/                         a copy of examples/disk-full-incident: catalog, playbooks, recordings
tests/                            the copy matches the example; config, approvals, refusals
```

The demo is deployed from this directory alone, so `incident/` is a copy
(a test fails when it drifts from the example) and Cloudfall comes from git
at a pinned rev.

## Run it

```console
uv sync
export CLOUDFALL_API_KEY=...          # a Nebius Token Factory key
uv run uvicorn cloudfall_demo.server:app --port 8765
```

Limits, all from the environment: `CLOUDFALL_DEMO_RUNS_PER_HOUR` per
visitor (6), `CLOUDFALL_DEMO_CONCURRENT_RUNS` (4) and
`CLOUDFALL_DEMO_DAILY_TOKENS` (3,000,000). Run directories are removed an
hour after they start.
