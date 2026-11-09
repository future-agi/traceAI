# Changelog

## [0.1.3](https://github.com/future-agi/traceAI/compare/npm-traceai-weaviate-v0.1.2...npm-traceai-weaviate-v0.1.3) (2026-11-05)


### Bug Fixes

* **npm:** point google-adk, livekit, pipecat and vertexai entry points at the emitted build ([b4414b1](https://github.com/future-agi/traceAI/commit/b4414b143fdc0b9c39d32a848f4ee1affd522e67))
* **npm:** release the entry-point repairs as 0.1.1 ([4c55284](https://github.com/future-agi/traceAI/commit/4c55284489203332176dbac3472b5b1da6fc7e97))
* **weaviate:** trace real TypeScript client and collection factories ([#469](https://github.com/future-agi/traceAI/issues/469)) ([4d1f490](https://github.com/future-agi/traceAI/commit/4d1f49005b480c504cafe451bdc4b56ff4ada66b))

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-weaviate-v0.1.1...npm-traceai-weaviate-v0.1.2) (2026-11-01)


### Bug Fixes

* **weaviate:** trace real TypeScript client and collection factories ([#469](https://github.com/future-agi/traceAI/issues/469)) ([4d1f490](https://github.com/future-agi/traceAI/commit/4d1f49005b480c504cafe451bdc4b56ff4ada66b))

## 0.1.1 (2026-10-29)

### Bug Fixes

* Repair package entry points and type declarations to target the emitted build. CommonJS `require()` and native Node `import()` now resolve the same CommonJS implementation with named exports.
