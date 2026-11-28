# Changelog

## [0.1.3](https://github.com/future-agi/traceAI/compare/npm-traceai-portkey-v0.1.2...npm-traceai-portkey-v0.1.3) (2026-11-28)


### Bug Fixes

* **typescript:** hide settings cover raw payloads, key families and whole-response output ([534a268](https://github.com/future-agi/traceAI/commit/534a2689cb4e61dfc25f5ae98205ffa22c00294a))

## [0.1.3] - pending release

### Fixed
* Inside the zero-code agent (@traceai/auto), the call's own request
  no longer arrives twice.

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-portkey-v0.1.1...npm-traceai-portkey-v0.1.2) (2026-11-05)


### Bug Fixes

* **portkey:** preserve privacy, effective inputs and request lifecycle ([#599](https://github.com/future-agi/traceAI/issues/599)) ([b51239f](https://github.com/future-agi/traceAI/commit/b51239f546540e09aeb2822c5d27c56c2ac0f3f3))
* **portkey:** trace real TypeScript SDK resources ([#435](https://github.com/future-agi/traceAI/issues/435)) ([12c2351](https://github.com/future-agi/traceAI/commit/12c23513d06ce4e5e45d3f87f6386e964d962350))

## 0.1.1 (2026-10-29)

### Bug Fixes

* Repair package entry points and type declarations to target the emitted build. CommonJS `require()` and native Node `import()` now resolve the same CommonJS implementation with named exports.
