# Changelog

## [0.1.1](https://github.com/future-agi/traceAI/compare/traceai-promptflow-v0.1.0...traceai-promptflow-v0.1.1) (2026-11-28)


### Bug Fixes

* **promptflow:** a stream read to the end is not marked incomplete when Prompt flow's finalization raises ([80ae3b4](https://github.com/future-agi/traceAI/commit/80ae3b4792f6aafd6f469d1c7b20dee36d9996ea))
* **promptflow:** trace promptflow-core installs zero-code and keep a failed stream's output ([#790](https://github.com/future-agi/traceAI/issues/790)) ([fc9f101](https://github.com/future-agi/traceAI/commit/fc9f10111291b9e7dd2c4b3b90f3e5e8ef6efc39))

## [Unreleased]

- `uninstrument()` also restores the Prompt flow names a module bound after `instrument()` (for example `from promptflow.tracing._trace import TracedIterator`), so no Prompt flow module keeps traceAI's replacements.
- When the installed Prompt flow has no `TracedIterator` or `TracedAsyncIterator`, `instrument()` skips that stream proxy with one warning naming the class; spans are still recorded, without ending the span of such a stream on close or when it is dropped.
- A stream that fails part-way keeps the output it yielded before the failure on its span (`output.value`, with `promptflow.stream.incomplete` set), next to `error.type`. `hide_outputs` hides that output, as for a stream that completes.
- A stream read to the end is not marked `promptflow.stream.incomplete` when Prompt flow's own end-of-stream handling raises afterwards. Its span keeps the items it yielded (`output.value`, hidden by `hide_outputs`), the `ERROR` status and `error.type`, and the exception reaches the application unchanged.
- Zero-code tracing (`fi-instrument`, `FI_AUTO_INSTRUMENT`) of an application that installs `promptflow-core` or `promptflow-tracing` without the full `promptflow` package needs the `fi-instrumentation-otel` release that turns the integration on at the first import of `promptflow.tracing`. With `fi-instrumentation-otel` 1.5.1 such an application is not traced and `fi-instrument doctor` reports the integration as pending; with the full `promptflow` package, 1.5.1 traces it too.

## [0.1.0] - pending release

- Instrument Microsoft Prompt flow (`promptflow-tracing`, `promptflow-core`): flex and DAG flow runs as `CHAIN` spans, nodes and `@trace` tools as child spans, LLM calls made through Prompt flow's OpenAI integration as `LLM` spans with model and token usage; sync, async and streaming.
