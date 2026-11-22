/**
 * Newly masked content keys (tool_call.function.arguments, message.function_call_arguments_json,
 * tool.description) written by the LangChain tracer through FITracer, driven by the real
 * instrumentation with hideInputs + hideOutputs. Fully offline: a local HTTP server stands in
 * for the model provider.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "@jest/globals";
import http from "http";
import { AddressInfo } from "net";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";
import { CallbackManager } from "@langchain/core/callbacks/manager";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { AIMessage, HumanMessage } from "@langchain/core/messages";
import { ChatOpenAI } from "@langchain/openai";
import { FITracer, REDACTED_VALUE, TraceConfigOptions } from "@traceai/fi-core";
import { LangChainInstrumentation } from "../instrumentation";

const SECRET_TOOL_ARGS = "synthetic-secret-tool-call-args-7731";
const SECRET_LEGACY_ARGS = "synthetic-secret-legacy-fn-args-4412";
const SECRET_TOOL_DESC = "synthetic-secret-tool-description-9085";
const SECRET_RESPONSE_ARGS = "synthetic-secret-response-args-5520";
const SECRETS = [SECRET_TOOL_ARGS, SECRET_LEGACY_ARGS, SECRET_TOOL_DESC, SECRET_RESPONSE_ARGS];

const KEY_RE = /tool_call\.function\.arguments|function_call_arguments_json|tool\.description/;

function exported(span: ReadableSpan): string {
  return JSON.stringify({
    name: span.name,
    attributes: span.attributes,
    events: span.events,
    status: span.status,
    links: span.links,
  });
}

describe("LangChain newly masked content keys under hideInputs/hideOutputs", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const instrumentation = new LangChainInstrumentation({ tracerProvider: provider });
  instrumentation.manuallyInstrument(CallbackManagerModule);
  let server: http.Server;
  let baseURL: string;

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      req.resume();
      req.on("end", () => {
        res.setHeader("content-type", "application/json");
        res.end(
          JSON.stringify({
            id: "chatcmpl-1",
            object: "chat.completion",
            created: 1,
            model: "gpt-4o-mini",
            choices: [
              {
                index: 0,
                message: {
                  role: "assistant",
                  content: null,
                  tool_calls: [
                    {
                      id: "call_resp",
                      type: "function",
                      function: {
                        name: "lookup",
                        arguments: JSON.stringify({ q: SECRET_RESPONSE_ARGS }),
                      },
                    },
                  ],
                },
                finish_reason: "tool_calls",
              },
            ],
            usage: { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 },
          }),
        );
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`;
  });
  afterAll(async () => {
    await new Promise((resolve) => server.close(resolve));
  });
  beforeEach(() => exporter.reset());

  function useTraceConfig(traceConfig: TraceConfigOptions) {
    (instrumentation as unknown as { fiTracer: FITracer }).fiTracer = new FITracer({
      tracer: provider.getTracer("test"),
      traceConfig,
    });
  }

  async function run() {
    const model = new ChatOpenAI({
      model: "gpt-4o-mini",
      apiKey: "test-key",
      maxRetries: 0,
      configuration: { baseURL },
    });
    const history = [
      new HumanMessage("hi"),
      new AIMessage({
        content: "",
        tool_calls: [{ id: "call_1", name: "lookup", args: { q: SECRET_TOOL_ARGS }, type: "tool_call" }],
        additional_kwargs: {
          function_call: { name: "lookup", arguments: JSON.stringify({ q: SECRET_LEGACY_ARGS }) },
        },
      }),
      new HumanMessage("continue"),
    ];
    await model.invoke(history);
    // A tool run whose serialized form carries a description
    const manager = await CallbackManager.configure([], [], undefined, undefined, undefined, undefined, undefined);
    const toolRun = await manager?.handleToolStart(
      { lc: 1, type: "not_implemented", id: ["tools", "lookup"], name: "lookup", description: SECRET_TOOL_DESC } as never,
      "input",
    );
    await toolRun?.handleToolEnd("done");
    await awaitAllCallbacks();
    return exporter.getFinishedSpans();
  }

  it("control: without hide flags the secrets are exported through the target keys", async () => {
    useTraceConfig({});
    const spans = await run();
    const text = spans.map(exported).join("\n");
    for (const secret of SECRETS) expect(text).toContain(secret);
    const keys = spans.flatMap((s) => Object.keys(s.attributes)).filter((k) => KEY_RE.test(k));
    expect(keys.length).toBeGreaterThan(0);
  });

  it("hideInputs + hideOutputs: target keys are absent or redacted and no secret is exported", async () => {
    useTraceConfig({ hideInputs: true, hideOutputs: true });
    const spans = await run();
    expect(spans.length).toBeGreaterThanOrEqual(2);
    for (const span of spans) {
      for (const [key, value] of Object.entries(span.attributes)) {
        if (KEY_RE.test(key)) expect([REDACTED_VALUE, undefined]).toContain(value);
      }
      for (const secret of SECRETS) expect(exported(span)).not.toContain(secret);
    }
  });
});
