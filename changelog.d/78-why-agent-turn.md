### Added

- `cloudfall why` tells which agent turn asked for a decision. Each step of
  an investigation record now keeps the `turn` that made its call (optional
  in `agent-investigation.schema.json`; older records have none). `why`
  reads the investigation records (`--investigations`, default
  `investigations`, served to MCP clients from the same place) and adds to a
  decision's story the model, the endpoint, the alert, the turn, and that
  turn's reasoning with the endpoint's response and request ids; the answer
  carries the same as `agent`. A decision a later call only repeated keeps
  the turn that asked first. An investigation record that is not valid JSON
  or does not match its schema stops `why` with `RECORD_INVALID`
  (`why_investigation_invalid`) (#78)
