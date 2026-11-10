# Changelog

All notable changes to `traceAI-tavily` will be documented here.

## [0.1.1](https://github.com/future-agi/traceAI/compare/traceai-tavily-v0.1.0...traceai-tavily-v0.1.1) (2026-11-05)


### Bug Fixes

* **tavily:** complete direct-client operation tracing ([#475](https://github.com/future-agi/traceAI/issues/475)) ([240ffa6](https://github.com/future-agi/traceAI/commit/240ffa64ba0c2e22b4153679a7154ee0aaba3d9c))
* **tavily:** keep extraction errors free of submitted URLs ([#566](https://github.com/future-agi/traceAI/issues/566)) ([cfc1f20](https://github.com/future-agi/traceAI/commit/cfc1f20a91e2bb3db32fbbdf37dd21184f83475e))
* **tavily:** preserve incomplete streams and gate README coverage ([#526](https://github.com/future-agi/traceAI/issues/526)) ([d0b8782](https://github.com/future-agi/traceAI/commit/d0b87823b2e3c8dccab594cb986d3e8e1d9bc3dc))
* **tavily:** protect endpoint credentials and redact PII before truncation ([#577](https://github.com/future-agi/traceAI/issues/577)) ([f3babef](https://github.com/future-agi/traceAI/commit/f3babefdd65c8eb5011aaa108510a8140d75cbce))

## [0.1.0] - Unreleased

### Fixed
- Plain `pytest` in the package directory now collects every test, as for the other traceAI Python
  packages: the three modules that use the shared harness put `python/tests` on `sys.path`
  themselves instead of relying on a `PYTHONPATH` (collection failed with
  `No module named 'harness'`).
- The exception event no longer carries the raw traceback (local file paths, username, code frames):
  `exception.stacktrace` is now the frames-free `<Type>: <message>` summary, with the key (and a hidden
  query) removed. Tests scan the span events in default and hidden-input modes.

### Verified
- Live 2026-10-17 against api.tavily.com (tavily-python 0.8.5): search, extract, keyless search and a bad-key
  error exported to a local Future AGI; stored rows scanned. `langchain-tavily` 0.2.18 `TavilySearch` run:
  not traced by this package.
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
