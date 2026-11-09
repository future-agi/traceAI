# Changelog

All notable changes to `traceAI-parallel` will be documented here.

## [0.1.1](https://github.com/future-agi/traceAI/compare/traceai-parallel-v0.1.0...traceai-parallel-v0.1.1) (2026-11-05)


### Bug Fixes

* **parallel:** cover Task workflows and Responses attribution ([#486](https://github.com/future-agi/traceAI/issues/486)) ([c10a016](https://github.com/future-agi/traceAI/commit/c10a0165d2b58c827844d3617c20bb19e832ed17))
* **parallel:** protect Responses privacy and interrupted workflows ([#562](https://github.com/future-agi/traceAI/issues/562)) ([b17c618](https://github.com/future-agi/traceAI/commit/b17c618a5cc910628e91e45a2cb08acc987c0b5d))

## [0.1.0] - Unreleased

### Security
- `exception.stacktrace` holds the exception line only. Traceback frames are never exported: they carry local
  file paths (usernames, virtualenv and host layout).

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
