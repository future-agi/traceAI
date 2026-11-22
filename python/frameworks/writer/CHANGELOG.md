# Changelog

## [Unreleased]

- Stream and response proxies keep passing `isinstance` checks against the SDK's `Stream`, `AsyncStream` and `httpx.Response`.
- A stream that is dropped without being read or closed is never ended by a finalizer. With the default batch processor nothing is exported for it; a processor that tracks open spans ends it at shutdown and marks it as left open.
- A stream that fails midway keeps the model, output and token counts received so far and is marked `writer.stream.incomplete`.

## [0.1.0] - 2026-11-19

- Instrument the Writer Python SDK (`writer-sdk`): chat (including tool calling and streaming) and text completions, synchronous and asynchronous.
