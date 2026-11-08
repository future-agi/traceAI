# Changelog

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-fi-instrumentation-google-adk-v0.1.1...npm-traceai-fi-instrumentation-google-adk-v0.1.2) (2026-11-05)


### Bug Fixes

* **google-adk:** protect native error history and retain cache usage ([#591](https://github.com/future-agi/traceAI/issues/591)) ([49aa721](https://github.com/future-agi/traceAI/commit/49aa7218f46f0e90248131afb30537ce681ff60c))
* **google-adk:** use native TypeScript tracing with privacy defaults ([#372](https://github.com/future-agi/traceAI/issues/372)) ([5c59f3d](https://github.com/future-agi/traceAI/commit/5c59f3de659f13d9c6b39f658c179479d509f7e3))

## 0.1.1 (2026-10-29)

### Bug Fixes

* Repair package entry points and type declarations to target the emitted build. CommonJS `require()` and native Node `import()` now resolve the same CommonJS implementation with named exports.
