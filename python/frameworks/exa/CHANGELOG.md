# Changelog

All notable changes to `traceAI-exa` will be documented here.

## [0.1.0] - Unreleased

### Added
- `ExaInstrumentor` for the `exa-py` client (`exa-py>=2.25.0,<3`): `search`, `get_contents`, `answer`,
  the deprecated `search_and_contents` alias, their async twins, and `stream_search` / `stream_answer`.
- Spans record the redacted query (capped at 1024 UTF-8 bytes) and document or citation counts; response
  bodies and the API key are never recorded.
- `get_contents` records `exa.url_count`; `instrument(capture_urls=True)` adds up to 20 redacted URLs in `exa.urls`.
- `TraceConfig` / `FI_*` environment support (`FI_HIDE_INPUTS`, PII redaction, `using_session` and friends).
- Key-safe error text capped at 1 KB, cancelled-call handling, and export of streams still open at
  interpreter exit.
- Spans use the repo's keys: `gen_ai.span.kind` (`RETRIEVER` for search, answer and streams; `TOOL` for
  `get_contents`), the query in `gen_ai.retrieval.query`, and counts in `exa.result_count` / `exa.url_count`.
- Only methods defined on each client class are wrapped, so `AsyncExa` can never wrap an inherited `Exa`
  method twice; a tracer that fails to start a span leaves the call untraced instead of raising.
- The exception event has `exception.type` and `exception.message` only; no stack trace (local paths) is exported.
- `pii_redaction` also applies to the error status and the exception event.
