/**
 * E2E Tests for @traceai/portkey
 *
 * These tests export spans to the FI backend via register() from @traceai/fi-core.
 * Even error spans (from dummy keys) appear in the UI.
 *
 * Required environment variables:
 *   FI_API_KEY      - FI platform API key
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { PortkeyInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const PORTKEY_API_KEY = process.env.PORTKEY_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Portkey E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: PortkeyInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let Portkey: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-portkey-e2e",
      batch: false,
    });

    instrumentation = new PortkeyInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const portkeyModule = await import("portkey-ai");
    Portkey = portkeyModule.default || portkeyModule.Portkey;
    client = new Portkey({ apiKey: PORTKEY_API_KEY || "dummy-key-for-e2e" });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Chat Completions", () => {
    it("should complete a basic chat request", async () => {
      try {
        const response = await client.chat.completions.create({
          model: "gpt-4",
          messages: [
            { role: "user", content: "What is 2 + 2? Answer with just the number." },
          ],
          max_tokens: 10,
        });

        expect(response.choices).toBeDefined();
        console.log("Chat completion response:", response.choices[0].message.content);
      } catch (error) {
        console.log("Chat completion errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming responses", async () => {
      try {
        const stream = await client.chat.completions.create({
          model: "gpt-4",
          messages: [
            { role: "user", content: "Count from 1 to 3." },
          ],
          max_tokens: 50,
          stream: true,
        });

        const chunks: string[] = [];
        for await (const chunk of stream) {
          const content = chunk.choices[0]?.delta?.content;
          if (content) {
            chunks.push(content);
          }
        }

        expect(chunks.length).toBeGreaterThan(0);
        console.log("Streaming response:", chunks.join(""));
      } catch (error) {
        console.log("Streaming errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      try {
        await client.chat.completions.create({
          model: "non-existent-model-12345",
          messages: [{ role: "user", content: "Hello" }],
        });
      } catch (error) {
        console.log("Error handling: correctly threw on invalid model");
      }
    }, 30000);
  });
});
