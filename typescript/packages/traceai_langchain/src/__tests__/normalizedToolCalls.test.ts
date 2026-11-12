import { describe, expect, it } from "@jest/globals";
import * as manager from "@langchain/core/callbacks/manager";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import * as chatModels from "@langchain/core/language_models/chat_models";
import { AIMessage, HumanMessage, ToolMessage } from "@langchain/core/messages";
import * as runnables from "@langchain/core/runnables";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import { LangChainInstrumentation } from "../instrumentation";
import { safelyFormatInputMessages, safelyFormatOutputMessages } from "../utils";

const normalizedCalls = [{
  type: "tool_call" as const,
  id: "call_demo",
  name: "lookup",
  args: { city: "Paris" },
}];
const canonicalCalls = [{
  id: "call_demo",
  function: { name: "lookup", arguments: JSON.stringify({ city: "Paris" }) },
}];
const legacyCalls = canonicalCalls.map((call) => ({ ...call, type: "function" }));

function aiMessage(
  kwargs: Record<string, unknown>,
  fields: Record<string, unknown> = {},
) {
  return {
    lc_id: ["langchain_core", "messages", "AIMessage"],
    lc_kwargs: { content: "", ...kwargs },
    ...fields,
  };
}

function serializedMessages(
  direction: "input" | "output",
  messages: Record<string, unknown>[],
) {
  const attributes = direction === "input"
    ? safelyFormatInputMessages({ messages: [messages] })
    : safelyFormatOutputMessages({ generations: [messages.map((message) => ({ message }))] });
  const key = direction === "input" ? SC.LLM_INPUT_MESSAGES : SC.LLM_OUTPUT_MESSAGES;
  const serialized = (attributes as Record<string, unknown> | null)?.[key];
  expect(typeof serialized).toBe("string");
  if (typeof serialized !== "string") throw new Error("Missing canonical messages");
  return JSON.parse(serialized);
}

const cases = [
  {
    name: "normalized lc_kwargs without additional kwargs",
    message: aiMessage({ tool_calls: normalizedCalls }),
  },
  {
    name: "normalized message fields",
    message: aiMessage({ additional_kwargs: {} }, { tool_calls: normalizedCalls }),
  },
  {
    name: "normalized fields without lc_kwargs",
    message: {
      lc_id: ["langchain_core", "messages", "AIMessage"],
      content: "",
      tool_calls: normalizedCalls,
    },
  },
  {
    name: "normalized calls instead of a duplicate legacy copy",
    message: aiMessage({
      tool_calls: normalizedCalls,
      additional_kwargs: { tool_calls: legacyCalls },
    }),
  },
  {
    name: "current message fields ahead of serialized and legacy copies",
    message: aiMessage({
      tool_calls: [{ name: "stale", args: { city: "Rome" }, id: "call_stale" }],
      additional_kwargs: { tool_calls: legacyCalls },
    }, { tool_calls: normalizedCalls }),
  },
  {
    name: "serialized normalized calls when message fields are empty",
    message: aiMessage({ tool_calls: normalizedCalls }, { tool_calls: [] }),
  },
  {
    name: "legacy fallback without normalized calls",
    message: aiMessage({ additional_kwargs: { tool_calls: legacyCalls } }),
  },
  {
    name: "legacy fallback when normalized arrays are empty",
    message: aiMessage({
      tool_calls: [],
      additional_kwargs: { tool_calls: legacyCalls },
    }, { tool_calls: [] }),
  },
];

describe("T7-TS-LANGCHAIN-001 normalized tool calls", () => {
  for (const direction of ["input", "output"] as const) {
    for (const { name, message } of cases) {
      it(`${direction} preserves ${name} without mutation or duplication`, () => {
        const before = JSON.stringify(message);
        expect(serializedMessages(direction, [message])).toEqual([
          { role: "assistant", tool_calls: canonicalCalls },
        ]);
        expect(JSON.stringify(message)).toBe(before);
      });
    }

    it(`${direction} serializes object arguments once and preserves call order and optional IDs`, () => {
      const args = { cities: ["Paris", "Rome"], options: { limit: 2 }, enabled: true };
      const message = aiMessage({ tool_calls: [
        { name: "lookup", args, id: "call_first" },
        { name: "finish", args: {} },
      ] });
      expect(serializedMessages(direction, [message])).toEqual([{
        role: "assistant",
        tool_calls: [
          { id: "call_first", function: { name: "lookup", arguments: JSON.stringify(args) } },
          { function: { name: "finish", arguments: "{}" } },
        ],
      }]);
    });

    for (const location of ["message", "lc_kwargs"] as const) {
      it(`${direction} preserves the tool result link from ${location}`, () => {
        const message = {
          lc_id: ["langchain_core", "messages", "ToolMessage"],
          lc_kwargs: {
            content: "Sunny",
            ...(location === "lc_kwargs" ? { tool_call_id: "call_demo" } : {}),
          },
          ...(location === "message" ? { tool_call_id: "call_demo" } : {}),
        };
        expect(serializedMessages(direction, [message])).toEqual([
          { role: "tool", content: "Sunny", tool_call_id: "call_demo" },
        ]);
      });
    }

    it(`${direction} retains legacy names and argument strings without an ID`, () => {
      const message = aiMessage({ additional_kwargs: { tool_calls: [{
        function: { name: "lookup", arguments: '{ "city": "Paris" }' },
      }] } });
      expect(serializedMessages(direction, [message])).toEqual([{
        role: "assistant",
        tool_calls: [{ function: { name: "lookup", arguments: '{ "city": "Paris" }' } }],
      }]);
    });
  }

  it("exports normalized-only BaseChatModel calls and their subsequent tool result links", async () => {
    // Fake the provider, not LangChain's message or callback machinery. No network.
    class ToolModel extends chatModels.BaseChatModel {
      constructor() { super({}); }
      _llmType() { return "offline-tool-model"; }
      async _generate() {
        return { generations: [{
          text: "",
          message: new AIMessage({ content: "", tool_calls: normalizedCalls }),
        }] };
      }
    }

    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    const instrumentation = new LangChainInstrumentation({
      tracerProvider: provider,
      instrumentationConfig: { enabled: false },
    });
    try {
      instrumentation.manuallyInstrument(manager, chatModels, runnables);
      const model = new ToolModel();
      const human = new HumanMessage("lookup Paris");
      const response = await model.invoke([human]);
      expect(response).toBeInstanceOf(AIMessage);
      if (!(response instanceof AIMessage)) throw new Error("Expected AIMessage");
      expect(response.tool_calls).toEqual(normalizedCalls);
      expect(response.additional_kwargs.tool_calls).toBeUndefined();

      await model.invoke([
        human,
        response,
        new ToolMessage({ content: "Sunny", tool_call_id: "call_demo" }),
      ]);
      await awaitAllCallbacks();
      await provider.forceFlush();

      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(2);
      expect(JSON.parse(spans[0].attributes[SC.LLM_OUTPUT_MESSAGES] as string)).toEqual([
        { role: "assistant", tool_calls: canonicalCalls },
      ]);
      expect(JSON.parse(spans[1].attributes[SC.LLM_INPUT_MESSAGES] as string)).toEqual([
        { role: "user", content: "lookup Paris" },
        { role: "assistant", tool_calls: canonicalCalls },
        { role: "tool", content: "Sunny", tool_call_id: "call_demo" },
      ]);
      expect(response.tool_calls).toEqual(normalizedCalls);
    } finally {
      await awaitAllCallbacks();
      instrumentation.disable();
      await provider.shutdown();
    }
  });
});
