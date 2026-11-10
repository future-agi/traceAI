# Changelog

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-fi-instrumentation-pipecat-v0.1.1...npm-traceai-fi-instrumentation-pipecat-v0.1.2) (2026-11-05)


### Bug Fixes

* **mastra:** enforce privacy and restore SDK compatibility ([#567](https://github.com/future-agi/traceAI/issues/567)) ([b4b0b8f](https://github.com/future-agi/traceAI/commit/b4b0b8feabd2ffd045e84a25210a613c5c972c68))
* **pipecat:** finalize cancelled connections and correct lifecycle spans ([#563](https://github.com/future-agi/traceAI/issues/563)) ([051a307](https://github.com/future-agi/traceAI/commit/051a3070db37a7eb59faeda05ca8ef15f54dc9ce))
* **pipecat:** restore TypeScript client tracing ([#479](https://github.com/future-agi/traceAI/issues/479)) ([ee5decd](https://github.com/future-agi/traceAI/commit/ee5decdd346f77cd30f2a0c7744de6d436dd0efa))

## 0.1.1 (2026-10-29)

### Bug Fixes

* Repair package entry points and type declarations to target the emitted build. CommonJS `require()` and native Node `import()` now resolve the same CommonJS implementation with named exports.
