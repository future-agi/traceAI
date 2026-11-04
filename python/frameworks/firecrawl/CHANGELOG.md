# Changelog

All notable changes to `traceAI-firecrawl` will be documented here.

## [0.1.0] - Unreleased

### Added
- `FirecrawlInstrumentor` for the Firecrawl v2 sync and async clients (`firecrawl-py>=4.46.2,<5`):
  `scrape`, `search`, `map`, `crawl`, `start_crawl`, `get_crawl_status` and `cancel_crawl`, one span per call.
- `gen_ai.span.kind` is `RETRIEVER` for `search` and `TOOL` for the other calls.
- `search` records the key-redacted query (capped at 1024 characters) in `gen_ai.retrieval.query` and
  `input.value`, and the result count in `firecrawl.result_count`; other calls record only the target host.
- Crawl job id, status, page count and credits; a crawl job that ends `failed` or `cancelled` sets ERROR.
- Fixed-vocabulary error metadata: no raw SDK error text or traceback is recorded.
- `TraceConfig` support (`hide_inputs`, `hide_input_text`, PII redaction, `using_session` and friends).
- `uninstrument()` also stops facades built while instrumented.
