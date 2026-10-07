# Spring AI compatibility notes (TH-7477)

The `traceai-spring-ai` wrapper is compiled and tested against Spring AI 1.1.x GA. Spring AI `1.0.0-M4`
is retired: the milestone API (`Message.getContent()`, `Usage.getGenerationTokens()`, `spring-ai-core`)
was removed before GA, so an artifact built on it fails at runtime on every current release.

## Closed matrix

| Cell | Spring AI | Spring Boot | Java | What runs |
|---|---|---|---|---|
| P | 1.1.8 | 3.5.15 | 17 | compile once, deterministic suite, example, real local agent |
| B | 1.1.0 | 3.5.7 | 17 | the same built JARs, common API only |

Other versions (Spring AI 1.0 GA, 2.0, Spring Boot 3.4 or 4, newer JDKs) are not claimed.

## What changed for callers

- Message and assistant text is read with `getText()`, not `getContent()`.
- Completion usage is read with `getCompletionTokens()`, not `getGenerationTokens()`.
- Depend on `org.springframework.ai:spring-ai-model` (the wrapper declares it `provided`).
- `TracedEmbeddingModel.getEmbeddingContent(Document)` delegates directly and exists only on 1.1.8+.
  Applications on 1.1.0 simply never call it; no reflection bridge is provided.

## Streaming

`stream(prompt)` starts no span and does not call the delegate until a subscriber subscribes. Each
subscription owns one span that ends exactly once. Streamed token usage is not aggregated. To bind a
parent span and TraceAI session/user attributes to a stream subscribed on another thread, use
`SpringAITracingContext.withContext(parent, attributes)` with `Flux.contextWrite`. Reusing one publisher
across tenants requires an explicit snapshot per subscription.

## Privacy

`hideInputs` and `hideInputMessages` are independent, as are the two output switches. Set both of a pair
to omit a channel completely. Metadata, session and user attributes are unaffected.
