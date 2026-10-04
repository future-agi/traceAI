// A Node "route" that answers one question with TanStack AI and traces it to
// Future AGI. Run: node src/chat.mjs "What is the weather in Paris?"
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

/** Answer one question. The adapter reads OPENAI_API_KEY and OPENAI_BASE_URL. */
export async function answer(question, middleware) {
  return chat({
    adapter: openaiChatCompletions(process.env.OPENAI_MODEL ?? "gpt-4o-mini"),
    systemPrompts: ["You are a concise weather assistant."],
    messages: [{ role: "user", content: question }],
    tools: [getWeather],
    middleware: [middleware],
    stream: false,
  });
}

/** The route: one traced chat() call, flushed in finally. */
export async function chatRoute(question, tracerProvider) {
  const middleware = futureAgiOtelMiddleware(
    tracerProvider.getTracer("tanstack-ai"),
  );
  try {
    return await answer(question, middleware);
  } finally {
    await flushTraces(tracerProvider);
  }
}

async function main() {
  const question = process.argv[2] ?? "What is the weather in Paris?";
  const tracerProvider = registerFutureAgiTracing();
  try {
    console.log(await chatRoute(question, tracerProvider));
  } finally {
    await shutdownTraces(tracerProvider);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
