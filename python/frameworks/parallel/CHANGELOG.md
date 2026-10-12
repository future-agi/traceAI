# Changelog

All notable changes to `traceAI-parallel` will be documented here.

## [0.1.0] - Unreleased

### Added
- `ParallelInstrumentor` for the Parallel web client (`parallel-web>=1.0.1,<2`): `search` and `extract` on
  `Parallel` and `AsyncParallel`, one span per call (`gen_ai.span.kind` `RETRIEVER` for `search`, `TOOL`
  for `extract`).
- The search queries in `gen_ai.retrieval.query` and `input.value` (key-redacted, about 1 KB); counts,
  ids, usage SKUs and warnings. Excerpts, titles, result URLs and page text are never recorded; requested
  URLs and the objective are opt-in (`capture_urls`, `capture_objective`).
- The API key is removed from every recorded value; `TraceConfig` `hide_inputs`, `hide_outputs` and
  `pii_redaction` apply to attributes, the error status, the exception event and warning messages.
- Errors set ERROR with the original exception re-raised; cancelled async calls end ERROR `cancelled`.
