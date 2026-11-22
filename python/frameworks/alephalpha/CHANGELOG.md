# Changelog

## [0.1.0] - pending release

- Instrument the Aleph Alpha Python SDK (`aleph-alpha-client`): complete, chat, embed, embeddings, semantic_embed, batch_semantic_embed, instructable_embed and evaluate on `Client` and `AsyncClient`, plus the async streaming variants `complete_with_streaming` and `chat_with_streaming`.
- A stream that fails part-way keeps the output received so far on its span (subject to the hide settings) together with the error status.
- When a completion returns several choices (`n` above 1), every choice's text is recorded as `gen_ai.output.messages.<index>.message.content` and every choice's finish reason is listed; `output.value` stays the text of choice 0.
