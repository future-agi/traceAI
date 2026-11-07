import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter, registerSecret } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "../instrumentation";

const sdk = require(process.env.OPENAI_TEST_MODULE || "openai");

test.each([false, true])("R-D-NATIVE-PARSER: real SDK chat streaming=%s binds only the parsed response root", async streaming => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new OpenAIInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
  try {
    for (const sensitive of [false, true]) {
      exporter.reset();
      const secret = "synthetic-d-native-echo-private";
      if (sensitive) registerSecret(secret);
      const parts = sensitive ? ["synthetic-d-", "native-echo-private"] : ["Hello.", " Again."];
      const logprobs = (token: string) => ({ content: [{ token, logprob: -0.1, bytes: [...token].map(c => c.charCodeAt(0)), top_logprobs: [{ token, logprob: -0.2 }] }] });
      const base = { id: "d-native", model: "synthetic-model", created: 1 };
      const body = streaming
        ? parts.map(part => `data: ${JSON.stringify({ ...base, object: "chat.completion.chunk", choices: [{ index: 0, delta: { role: "assistant", content: part }, logprobs: logprobs(part), finish_reason: null }] })}\n\n`).join("") + "data: [DONE]\n\n"
        : JSON.stringify({ ...base, object: "chat.completion", choices: [{ index: 0, message: { role: "assistant", content: parts.join("") }, logprobs: { content: parts.flatMap(part => logprobs(part).content) }, finish_reason: "stop" }] });
      const fetch = jest.fn(async () => new Response(body, { headers: { "content-type": streaming ? "text/event-stream" : "application/json" } }));
      const client = new sdk.OpenAI({ apiKey: "synthetic-unused-key", maxRetries: 0, fetch });
      const result = await client.chat.completions.create({ model: "synthetic-model", messages: [{ role: "user", content: "hello" }], stream: streaming });
      if (streaming) {
        const seen: any[] = [];
        for await (const chunk of result) seen.push(chunk);
        // Instrumentation must not mask or mutate data returned to the caller.
        expect(seen.map(chunk => chunk.choices[0].delta.content).join("")).toBe(parts.join(""));
      } else expect(result.choices[0].message.content).toBe(parts.join(""));
      await provider.forceFlush();
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      const clean = privacyFilter().span(exporter.getFinishedSpans()[0]);
      const raw = JSON.parse(clean.attributes["raw.output"] as string);
      const choices = streaming ? raw.flatMap((chunk: any) => chunk.choices) : raw.choices;
      expect(choices.length).toBeGreaterThan(0);
      if (sensitive) for (const choice of choices) expect(choice.logprobs).toBe("<redacted>");
      else expect(choices[0].logprobs.content[0].token).toBe("Hello.");
    }
  } finally { instrumentation.disable(); await provider.shutdown(); }
});
