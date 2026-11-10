# Changelog

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-mongodb-v0.1.1...npm-traceai-mongodb-v0.1.2) (2026-11-05)


### Bug Fixes

* **mongodb:** preserve bulk-write errors and reentrant span lifetimes ([#597](https://github.com/future-agi/traceAI/issues/597)) ([b8d56fa](https://github.com/future-agi/traceAI/commit/b8d56fa8c9773bd4bbc2e9eb8f0275adf9a4f884))
* **mongodb:** preserve TypeScript cursor APIs with command tracing ([#400](https://github.com/future-agi/traceAI/issues/400)) ([619164d](https://github.com/future-agi/traceAI/commit/619164d491babb80ad049e96d4e2a6e8b0de3b5d))

## 0.1.1 (2026-10-29)

### Bug Fixes

* Repair package entry points and type declarations to target the emitted build. CommonJS `require()` and native Node `import()` now resolve the same CommonJS implementation with named exports.
