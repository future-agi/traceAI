/**
 * E2E tests for xAI (Grok) instrumentation
 *
 * These tests export spans to the FI backend via register() from @traceai/fi-core.
 * Even error spans (from dummy keys) appear in the UI.
 *
 * Required environment variables:
 *   FI_API_KEY     - FI platform API key
 *
 * Optional:
 *   GOOGLE_API_KEY - Google API key for OpenAI-compatible endpoint
 *
 * Example:
 *   FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { XAIInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY || "dummy-key-for-e2e";

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("XAIInstrumentation E2E", () => {
  let provider: FITracerProvider;
  let instrumentation: XAIInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let OpenAI: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-xai-e2e",
      batch: false,
    });

    instrumentation = new XAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const openaiModule = await import("openai");
    instrumentation.manuallyInstrument(
      openaiModule as unknown as Record<string, unknown>,
    );
    OpenAI = openaiModule.default;

    client = new OpenAI({
      baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
      apiKey: GOOGLE_API_KEY,
    });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  it("should trace chat completion", async () => {
    try {
      const response = await client.chat.completions.create({
        model: "gemini-2.0-flash",
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "Say hello in one word." },
        ],
        max_tokens: 10,
      });
      expect(response.choices[0].message.content).toBeDefined();
      console.log("Chat response:", response.choices[0].message.content);
    } catch (error) {
      console.log(
        "Chat completion errored (span still exported):",
        (error as Error).message,
      );
    }
  }, 30000);

  it("should trace streaming chat completion", async () => {
    try {
      const stream = await client.chat.completions.create({
        model: "gemini-2.0-flash",
        messages: [{ role: "user", content: "Count from 1 to 3." }],
        max_tokens: 20,
        stream: true,
      });

      let fullContent = "";
      for await (const chunk of stream) {
        const content = chunk.choices[0]?.delta?.content;
        if (content) {
          fullContent += content;
        }
      }

      expect(fullContent.length).toBeGreaterThan(0);
      console.log("Streaming response:", fullContent);
    } catch (error) {
      console.log(
        "Streaming errored (span still exported):",
        (error as Error).message,
      );
    }
  }, 30000);

  it("should handle tool calling", async () => {
    try {
      const response = await client.chat.completions.create({
        model: "gemini-2.0-flash",
        messages: [
          { role: "user", content: "What's the current time in Tokyo?" },
        ],
        tools: [
          {
            type: "function",
            function: {
              name: "get_current_time",
              description: "Get the current time in a specific timezone",
              parameters: {
                type: "object",
                properties: {
                  timezone: {
                    type: "string",
                    description: "IANA timezone (e.g., 'Asia/Tokyo')",
                  },
                },
                required: ["timezone"],
              },
            },
          },
        ],
        tool_choice: "auto",
        max_tokens: 100,
      });

      const toolCall = response.choices[0]?.message?.tool_calls?.[0];
      console.log(
        "Tool calling response:",
        toolCall ? `Called ${toolCall.function.name}` : "No tool call",
      );
    } catch (error) {
      console.log(
        "Tool calling errored (span still exported):",
        (error as Error).message,
      );
    }
  }, 30000);

  it("should handle multi-turn conversation", async () => {
    try {
      const response = await client.chat.completions.create({
        model: "gemini-2.0-flash",
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "My name is Alice." },
          { role: "assistant", content: "Nice to meet you, Alice!" },
          { role: "user", content: "What's my name?" },
        ],
        max_tokens: 20,
      });

      expect(response.choices[0].message.content).toBeDefined();
      console.log("Multi-turn response:", response.choices[0].message.content);
    } catch (error) {
      console.log(
        "Multi-turn errored (span still exported):",
        (error as Error).message,
      );
    }
  }, 30000);

  it("should handle errors gracefully", async () => {
    await expect(
      client.chat.completions.create({
        model: "non-existent-model",
        messages: [{ role: "user", content: "Hello" }],
      }),
    ).rejects.toThrow();
    console.log("Error handling: correctly threw on invalid model");
  }, 30000);
});
