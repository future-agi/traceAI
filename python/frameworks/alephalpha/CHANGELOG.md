# Changelog

## [Unreleased]

- `output.value` is the text of choice 0 only. When choice 0 has no text, `output.value` is not set, even if another choice has text.
- A completion with more than one choice, or a streamed completion with a chunk or summary for a choice other than choice 0, records each choice's text as `gen_ai.output.messages.<index>.message.content`, including when only one of the choices has text. The hide settings that hide output text hide these too.
- A stream that fails part-way keeps the output received so far on its span (subject to the hide settings) together with the error status.
- When a completion returns several choices (`n` above 1), every choice's finish reason is listed.

## [0.1.0] - 2026-11-20

- Instrument the Aleph Alpha Python SDK (`aleph-alpha-client`): complete, chat, embed, embeddings, semantic_embed, batch_semantic_embed, instructable_embed and evaluate on `Client` and `AsyncClient`, plus the async streaming variants `complete_with_streaming` and `chat_with_streaming`.
