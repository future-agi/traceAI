// Test fixture: abort a streaming chat() after the first text delta, with the
// recipe's middleware, and report how the stream ended.
import { chat } from "@tanstack/ai";
import { openaiChatCompletions } from "@tanstack/ai-openai";
import {
  flushTraces,
  futureAgiOtelMiddleware,
  registerFutureAgiTracing,
  shutdownTraces,
} from "../src/tracing.mjs";

const tracerProvider = registerFutureAgiTracing();
const abortController = new AbortController();
let outcome = "completed";
try {
  const stream = chat({
    adapter: openaiChatCompletions(process.env.OPENAI_MODEL ?? "gpt-4o-mini"),
    messages: [{ role: "user", content: process.argv[2] }],
    middleware: [futureAgiOtelMiddleware(tracerProvider.getTracer("abort"))],
    abortController,
  });
  for await (const chunk of stream) {
    if (chunk.type === "TEXT_MESSAGE_CONTENT") abortController.abort();
  }
} catch (error) {
  outcome = `threw ${error?.name ?? "error"}`;
} finally {
  await flushTraces(tracerProvider);
  await shutdownTraces(tracerProvider);
}
console.log(`outcome=${outcome}`);
