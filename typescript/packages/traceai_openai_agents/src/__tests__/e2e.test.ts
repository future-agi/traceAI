/**
 * E2E Tests for @traceai/openai-agents
 *
 * These tests run against the OpenAI Agents SDK and export spans to the FI backend.
 * Set FI_API_KEY and either OPENAI_API_KEY or GOOGLE_API_KEY environment variable to run.
 *
 * Run with: FI_API_KEY=... OPENAI_API_KEY=your_key pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider } from "@traceai/fi-core";
import { OpenAIAgentsInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("OpenAI Agents E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: OpenAIAgentsInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-openai-agents-e2e",
      batch: false,
    });

    instrumentation = new OpenAIAgentsInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Agent", () => {
    it("should create and run a basic agent", async () => {
      const agentsModule = await import("@openai/agents");
      const { Agent, run } = agentsModule;

      const agent = new Agent({
        name: "test-agent",
        instructions: "You are a helpful assistant. Answer briefly.",
        model: OPENAI_API_KEY ? "gpt-4o-mini" : "gemini-2.0-flash",
      });

      const result = await run(agent, "What is 2 + 2? Answer with just the number.");
      expect(result).toBeDefined();
      expect(result.finalOutput).toBeDefined();
    }, 60000);

    it("should handle agent with tools", async () => {
      const agentsModule = await import("@openai/agents");
      const { Agent, run } = agentsModule;

      const agent = new Agent({
        name: "tool-agent",
        instructions: "You are a helpful assistant.",
        model: OPENAI_API_KEY ? "gpt-4o-mini" : "gemini-2.0-flash",
        tools: [
          {
            name: "get_time",
            description: "Get the current time",
            parameters: { type: "object", properties: {} },
            execute: async () => new Date().toISOString(),
          },
        ],
      });

      const result = await run(agent, "What time is it?");
      expect(result).toBeDefined();
    }, 60000);
  });

  describe("Error Handling", () => {
    it("should handle invalid configuration gracefully", async () => {
      const agentsModule = await import("@openai/agents");
      const { Agent, run } = agentsModule;

      const agent = new Agent({
        name: "error-agent",
        instructions: "Test agent.",
        model: "non-existent-model-12345",
      });

      await expect(
        run(agent, "Hello")
      ).rejects.toThrow();
    }, 30000);
  });
});
