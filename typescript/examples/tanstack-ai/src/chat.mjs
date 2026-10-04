// A Node "route" that answers one question with TanStack AI and traces it to
// Future AGI. Run: node src/chat.mjs "What is the weather in Paris?" [thread-id]
// A thread id (the caller's conversation id) becomes the Future AGI session.
//
// Server side only. FI_API_KEY and FI_SECRET_KEY must never enter a browser
// bundle.
import { pathToFileURL } from "node:url";
import { chat, toolDefinition } from "@tanstack/ai";
import { openaiChatCompletions } from "@tanstack/ai-openai";
import {
  flushTraces,
  futureAgiOtelMiddleware,
  registerFutureAgiTracing,
  shutdownTraces,
} from "./tracing.mjs";

const getWeather = toolDefinition({
  name: "get_weather",
  description: "Get the current weather for a city.",
  inputSchema: {
    type: "object",
    properties: { city: { type: "string" } },
    required: ["city"],
  },
}).server(async ({ city }) => ({ city, forecast: "sunny", celsius: 21 }));

/**
 * Answer one question. The adapter reads OPENAI_API_KEY and OPENAI_BASE_URL.
 *
 * Reads chat()'s stream to the end, even after a RUN_ERROR chunk, and only
 * then throws. Do not use `stream: false` with otelMiddleware: at
 * @tanstack/ai 0.64.0 it collects text with streamToText, which throws at
 * the first RUN_ERROR and stops reading. The run then never reaches its
 * onError hook, so the chat and model-call spans are never ended or exported
 * and a failed request leaves no trace.
 */
export async function answer(question, middleware, { threadId } = {}) {
  const stream = chat({
    adapter: openaiChatCompletions(process.env.OPENAI_MODEL ?? "gpt-4o-mini"),
    systemPrompts: ["You are a concise weather assistant."],
    messages: [{ role: "user", content: question }],
    tools: [getWeather],
    middleware: [middleware],
    ...(threadId ? { threadId } : {}),
  });
  let text = "";
  let runError = null;
  for await (const chunk of stream) {
    if (chunk.type === "RUN_ERROR") {
      runError ??= chunk;
    } else if (chunk.type === "TEXT_MESSAGE_CONTENT" && chunk.delta) {
      text += chunk.delta;
    }
  }
  if (runError) {
    throw new Error(runError.message || runError.error?.message || "chat failed");
  }
  return text;
}

/**
 * The route: one traced chat() call, flushed in finally. `threadId` is the
 * caller's conversation id; when given, it is chat()'s threadId and every
 * span's session.id.
 */
export async function chatRoute(question, tracerProvider, { threadId } = {}) {
  const middleware = futureAgiOtelMiddleware(
    tracerProvider.getTracer("tanstack-ai"),
    { threadIdAsSession: Boolean(threadId) },
  );
  try {
    return await answer(question, middleware, { threadId });
  } finally {
    await flushTraces(tracerProvider);
  }
}

async function main() {
  const question = process.argv[2] ?? "What is the weather in Paris?";
  const threadId = process.argv[3];
  const tracerProvider = registerFutureAgiTracing();
  try {
    console.log(await chatRoute(question, tracerProvider, { threadId }));
  } catch (error) {
    // The route's error response. The spans are already exported.
    console.error(`chat failed: ${error?.message ?? String(error)}`);
    process.exitCode = 1;
  } finally {
    await shutdownTraces(tracerProvider);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
