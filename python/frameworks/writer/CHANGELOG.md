# Changelog

## [Unreleased]

- Stream and response proxies keep passing `isinstance` checks against the SDK's `Stream`, `AsyncStream` and `httpx.Response`.
- A stream that is dropped without being read or closed exports no span; no finalizer runs the exporter.

## [0.1.0] - 2026-11-19

- Instrument the Writer Python SDK (`writer-sdk`): chat (including tool calling and streaming) and text completions, synchronous and asynchronous.
