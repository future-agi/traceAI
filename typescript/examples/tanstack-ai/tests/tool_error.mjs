// Test fixture: chat() with a get_weather tool that throws an error whose
// message carries a marker and a local file path, through the recipe's
// middleware. Pass "capture" as the second argument to turn on the content
// opt-in. Prints how the run ended.
import { chat, toolDefinition } from "@tanstack/ai";
import { openaiChatCompletions } from "@tanstack/ai-openai";
import {
  flushTraces,
  futureAgiOtelMiddleware,
  registerFutureAgiTracing,
  shutdownTraces,
} from "../src/tracing.mjs";

export const TOOL_ERROR = "TOOLERR-MARKER-8e1f cannot read /home/th8238-user/private/notes.json";

const failingTool = toolDefinition({
  name: "get_weather",
  description: "Get the current weather for a city.",
  inputSchema: {
    type: "object",
    properties: { city: { type: "string" } },
    required: ["city"],
  },
}).server(async () => {
  throw new Error(TOOL_ERROR);
});

const [question, mode] = process.argv.slice(2);
const tracerProvider = registerFutureAgiTracing();
let outcome = "answered";
try {
  const middleware = futureAgiOtelMiddleware(tracerProvider.getTracer("tool-error"), {
    captureContent: mode === "capture",
  });
  for await (const chunk of chat({
    adapter: openaiChatCompletions(process.env.OPENAI_MODEL ?? "gpt-4o-mini"),
    messages: [{ role: "user", content: question }],
    tools: [failingTool],
    middleware: [middleware],
  })) {
    if (chunk.type === "RUN_ERROR") outcome = "run_error";
  }
} finally {
  await flushTraces(tracerProvider);
  await shutdownTraces(tracerProvider);
}
console.log(`outcome=${outcome}`);
