# Changelog

## [0.1.1](https://github.com/future-agi/traceAI/compare/traceai-alephalpha-v0.1.0...traceai-alephalpha-v0.1.1) (2026-11-28)


### Bug Fixes

* **alephalpha:** output.value takes choice 0's text only and every multi-choice response records per-index content ([7db6bbe](https://github.com/future-agi/traceAI/commit/7db6bbea45f7d2c3a12e64c6cd4f98b147b1cc2a))
* **alephalpha:** record every completion and keep a failed stream's partial output ([#788](https://github.com/future-agi/traceAI/issues/788)) ([1e64e89](https://github.com/future-agi/traceAI/commit/1e64e894f64989d74252b9a6466bb50137c284c4))

## [Unreleased]

- `output.value` is the text of choice 0 only. When choice 0 has no text, `output.value` is not set, even if another choice has text.
- A completion with more than one choice, or a streamed completion with a chunk or summary for a choice other than choice 0, records each choice's text as `gen_ai.output.messages.<index>.message.content`, including when only one of the choices has text. The hide settings that hide output text hide these too.

## [0.1.0] - pending release

- Instrument the Aleph Alpha Python SDK (`aleph-alpha-client`): complete, chat, embed, embeddings, semantic_embed, batch_semantic_embed, instructable_embed and evaluate on `Client` and `AsyncClient`, plus the async streaming variants `complete_with_streaming` and `chat_with_streaming`.
- A stream that fails part-way keeps the output received so far on its span (subject to the hide settings) together with the error status.
- When a completion returns several choices (`n` above 1), every choice's text is recorded as `gen_ai.output.messages.<index>.message.content` and every choice's finish reason is listed; `output.value` stays the text of choice 0.
