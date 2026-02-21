/**
 * E2E tests for OpenAI instrumentation
 *
 * These tests use Google's OpenAI-compatible endpoint and export spans
 * to the FI backend via register() from @traceai/fi-core.
 *
 * Required environment variables:
 *   FI_API_KEY      - FI platform API key
 *   FI_SECRET_KEY   - FI platform secret key (if required)
 *   GOOGLE_API_KEY  - Google API key for OpenAI-compatible endpoint
 *
 * Example:
 *   FI_API_KEY=... GOOGLE_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("OpenAIInstrumentation E2E", () => {
  let provider: FITracerProvider;
  let instrumentation: OpenAIInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let OpenAI: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-openai-e2e",
      batch: false,
    });

    instrumentation = new OpenAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const openaiModule = await import("openai");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    instrumentation.manuallyInstrument(openaiModule as any);
    OpenAI = openaiModule.default;

    client = new OpenAI({
      baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
      apiKey: GOOGLE_API_KEY || "dummy-key-for-e2e",
    });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  it("should trace chat completion", async () => {
    const response = await client.chat.completions.create({
      model: "gemini-2.0-flash",
      messages: [
        { role: "system", content: "You are a helpful assistant." },
        { role: "user", content: "Say hello in one word." },
      ],
      max_tokens: 10,
    });

    expect(response.choices[0].message.content).toBeDefined();
    console.log("Chat completion response:", response.choices[0].message.content);
  }, 30000);

  it("should trace streaming chat completion", async () => {
    const stream = await client.chat.completions.create({
      model: "gemini-2.0-flash",
      messages: [
        { role: "user", content: "Count from 1 to 3." },
      ],
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
  }, 30000);

  it("should handle tool calling", async () => {
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
                timezone: { type: "string", description: "IANA timezone (e.g., 'Asia/Tokyo')" },
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
    expect(response.choices[0]).toBeDefined();
    console.log("Tool calling response:", toolCall ? `Called ${toolCall.function.name}` : "No tool call");
  }, 30000);

  it("should handle multi-turn conversation", async () => {
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
    expect(response.choices[0].message.content.toLowerCase()).toContain("alice");
    console.log("Multi-turn response:", response.choices[0].message.content);
  }, 30000);

  it("should handle errors gracefully", async () => {
    await expect(
      client.chat.completions.create({
        model: "non-existent-model",
        messages: [{ role: "user", content: "Hello" }],
      })
    ).rejects.toThrow();
    console.log("Error handling: correctly threw on invalid model");
  }, 30000);
});
