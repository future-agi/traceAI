# Changelog

## [0.1.3](https://github.com/future-agi/traceAI/compare/npm-traceai-redis-v0.1.2...npm-traceai-redis-v0.1.3) (2026-11-05)


### Bug Fixes

* **npm:** point google-adk, livekit, pipecat and vertexai entry points at the emitted build ([b4414b1](https://github.com/future-agi/traceAI/commit/b4414b143fdc0b9c39d32a848f4ee1affd522e67))
* **npm:** release the entry-point repairs as 0.1.1 ([4c55284](https://github.com/future-agi/traceAI/commit/4c55284489203332176dbac3472b5b1da6fc7e97))
* **redis:** make TypeScript tracing reversible and ESM-safe ([#450](https://github.com/future-agi/traceAI/issues/450)) ([dc74bb4](https://github.com/future-agi/traceAI/commit/dc74bb48e51de06244d9d486cf451a53678501b1))

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-redis-v0.1.1...npm-traceai-redis-v0.1.2) (2026-11-01)


### Bug Fixes

* **redis:** make TypeScript tracing reversible and ESM-safe ([#450](https://github.com/future-agi/traceAI/issues/450)) ([dc74bb4](https://github.com/future-agi/traceAI/commit/dc74bb48e51de06244d9d486cf451a53678501b1))

## 0.1.1 (2026-10-29)

### Bug Fixes

* Repair package entry points and type declarations to target the emitted build. CommonJS `require()` and native Node `import()` now resolve the same CommonJS implementation with named exports.
