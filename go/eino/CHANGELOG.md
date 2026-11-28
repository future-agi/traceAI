# Changelog

## [0.1.1](https://github.com/future-agi/traceAI/compare/go/eino/v0.1.0...go/eino/v0.1.1) (2026-11-28)


### Features

* **go:** Eino blank import — wire-equal masking, per-run dedup, bounded activation and shutdown, release gate ([d0e882c](https://github.com/future-agi/traceAI/commit/d0e882cba848a1dd49158362c1448c475b823899))
* **go:** trace Eino from one blank import ([9cf127a](https://github.com/future-agi/traceAI/commit/9cf127a64ca98415ba00a87c78191eb0defd364f))
* **go:** trace Eino from one blank import through the agent's route ([711e8a9](https://github.com/future-agi/traceAI/commit/711e8a9720ffd2ed627b59561f55b5a7da364ae0))


### Bug Fixes

* **go/auto:** events and links of the Future AGI copy are the ones the SDK kept; a value over the length limit is replaced whole before the cut; the record is bounded ([94c67e6](https://github.com/future-agi/traceAI/commit/94c67e607c0e7de36804542d9964008d36f349f4))
* **go/auto:** events, links and status of a module span are kept raw and replaced with the end-time and export-time snapshots; replace, then cap; one stack trace capture ([fa6ebdd](https://github.com/future-agi/traceAI/commit/fa6ebddd9f15ae7b5f05fec8eece81a03e8f7151))
* **go/auto:** retriever content follows the content switches; known secrets are replaced in the application's copy at the end of the span and in Future AGI's at export; RecordError like the SDK ([b7c4328](https://github.com/future-agi/traceAI/commit/b7c4328080f3186db59611d8905910f5d7c46825))
