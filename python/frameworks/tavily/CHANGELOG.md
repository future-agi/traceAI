# Changelog

All notable changes to `traceAI-tavily` will be documented here.

## [0.1.0] - Unreleased

### Verified
- Re-verified 2026-10-16 against tavily-python 0.8.4 and 0.8.5 (latest): full suite, plus an end-to-end run
  into a local Future AGI deployment. 0.8.5 renames `search`'s `max_hours` to `max_age_hours`; a test covers both.

### Added
- `TavilyInstrumentor` for `TavilyClient` and `AsyncTavilyClient` (`tavily-python>=0.8.4,<1`): one span per
  `search` (`tavily.search`, `gen_ai.span.kind=RETRIEVER`) and `extract` (`tavily.extract`, `TOOL`).
- The key-redacted query (capped at 1024 UTF-8 bytes) in `input.value`, and for `search` also in
  `gen_ai.retrieval.query`; URL, result and failed-result counts. URLs and response content are never recorded.
- The API key is removed from every recorded text wherever the client keeps it.
- `TraceConfig` support: `hide_inputs` also removes the query from error text; `pii_redaction` also
  covers the error status and the exception event.
- Errors set ERROR with the original exception re-raised; cancelled async calls end ERROR `cancelled`.
- Measurement tests showing the bare client emits nothing on its own and the LangChain tool path is
  already traced by `traceAI-langchain`.
