/**
 * Content and error-detail hiding through the real LangChain instrumentation (the patched
 * CallbackManager injects the tracer), with a real ChatOpenAI against a local HTTP server.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "@jest/globals";
import http from "http";
import { AddressInfo } from "net";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { ChatPromptTemplate } from "@langchain/core/prompts";
import { RunnableLambda } from "@langchain/core/runnables";
import { ChatOpenAI } from "@langchain/openai";
import { FITracer, REDACTED_VALUE, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { LangChainInstrumentation } from "../instrumentation";

const PROMPT = "my secret prompt: the launch code is 0000";
const MISSING_MODEL = "missing-model";

function chatCompletion(model: string) {
  return {
    id: "chatcmpl-1",
    object: "chat.completion",
    created: 1,
    model,
    choices: [{ index: 0, message: { role: "assistant", content: "Sure." }, finish_reason: "stop" }],
    usage: { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 },
  };
}

/** Everything the exporter is handed for a span, as text */
function exported(span: ReadableSpan): string {
  return JSON.stringify({
    name: span.name,
    attributes: span.attributes,
    events: span.events,
    status: span.status,
    links: span.links,
  });
}

describe("LangChain instrumentation with hidden content", () => {
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
      let raw = "";
      req.on("data", (part) => (raw += part));
      req.on("end", () => {
        const body = JSON.parse(raw || "{}");
        res.setHeader("content-type", "application/json");
        if (body.model === MISSING_MODEL) {
          // The provider error body quotes the request
          res.statusCode = 404;
          res.end(
            JSON.stringify({
              error: {
                message: `The model '${MISSING_MODEL}' does not exist for input "${PROMPT}"`,
                type: "invalid_request_error",
                param: null,
                code: "model_not_found",
              },
            }),
          );
          return;
        }
        res.end(JSON.stringify(chatCompletion(body.model)));
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`;
  });
  afterAll(async () => {
    await new Promise((resolve) => server.close(resolve));
  });
  beforeEach(() => {
    exporter.reset();
  });

  /** The patched CallbackManager reads the instrumentation's FITracer on every run */
  function useTraceConfig(traceConfig: TraceConfigOptions) {
    (instrumentation as unknown as { fiTracer: FITracer }).fiTracer = new FITracer({
      tracer: provider.getTracer("test"),
      traceConfig,
    });
  }

  function chatModel(model: string) {
    return new ChatOpenAI({ model, apiKey: "test-key", maxRetries: 0, configuration: { baseURL } });
  }

  /** An LLM success, an LLM failure quoting the prompt, and a step that throws with the prompt */
  async function promptOnlyInInputs() {
    await chatModel("gpt-4o-mini").invoke(PROMPT);
    await expect(chatModel(MISSING_MODEL).invoke(PROMPT)).rejects.toThrow("does not exist");
    const failingStep = RunnableLambda.from(async (input: string) => {
      throw new Error(`could not handle "${input}"`);
    }).withConfig({ runName: "failing_step" });
    await expect(failingStep.invoke(PROMPT)).rejects.toThrow("could not handle");
    await awaitAllCallbacks();
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(3);
    return spans;
  }

  /** A prompt template (whose output is the formatted prompt) piped into the model */
  async function templateChain(model: string) {
    const chain = ChatPromptTemplate.fromTemplate("{question}")
      .pipe(chatModel(model))
      .withConfig({ runName: "template_chain" });
    await chain.invoke({ question: PROMPT }).catch(() => undefined);
    await awaitAllCallbacks();
    return exporter.getFinishedSpans();
  }

  it("control: without hide flags the prompt is exported (the check can see it)", async () => {
    useTraceConfig({});
    for (const span of await promptOnlyInInputs()) {
      expect(exported(span)).toContain(PROMPT);
    }
  });

  it("hideInputs: the prompt is in no exported attribute, event or status", async () => {
    useTraceConfig({ hideInputs: true });
    const spans = await promptOnlyInInputs();
    for (const span of spans) {
      expect(exported(span)).not.toContain(PROMPT);
      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe(REDACTED_VALUE);
    }
    const failed = spans.filter((span) => span.status.code === SpanStatusCode.ERROR);
    expect(failed).toHaveLength(2);
    for (const span of failed) {
      expect(span.status).toEqual({ code: SpanStatusCode.ERROR });
      expect(span.attributes["error.type"]).toBeDefined();
      for (const event of span.events.filter((e) => e.name === "exception")) {
        expect(Object.keys(event.attributes ?? {})).toEqual(["exception.type"]);
      }
    }
    const llm = spans.find((span) => span.status.code !== SpanStatusCode.ERROR);
    expect(String(llm?.attributes[SemanticConventions.OUTPUT_VALUE])).toContain("Sure.");
  });

  it("hideInputs: a template's variables are hidden; only the template's output shows the prompt", async () => {
    useTraceConfig({ hideInputs: true });
    const spans = await templateChain("gpt-4o-mini");
    expect(spans.map((span) => span.name).sort()).toEqual(
      ["ChatOpenAI", "ChatPromptTemplate", "template_chain"].sort(),
    );
    const template = spans.find((span) => span.name === "ChatPromptTemplate") as ReadableSpan;
    expect(template.attributes[SemanticConventions.PROMPT_TEMPLATE_VARIABLES]).toBe(
      REDACTED_VALUE,
    );
    // The formatted prompt is the template's output, which hideInputs does not hide
    const leaking = Object.entries(template.attributes)
      .filter(([, value]) => String(value).includes(PROMPT))
      .map(([key]) => key);
    expect(leaking).toEqual([SemanticConventions.OUTPUT_VALUE]);
    for (const span of spans.filter((s) => s !== template)) {
      expect(exported(span)).not.toContain(PROMPT);
    }
  });

  it.each([["success", "gpt-4o-mini"], ["failure", MISSING_MODEL]])(
    "hideInputs + hideOutputs: a template chain (%s) exports no prompt at all",
    async (_label, model) => {
      useTraceConfig({ hideInputs: true, hideOutputs: true });
      const spans = await templateChain(model);
      expect(spans).toHaveLength(3);
      for (const span of spans) {
        expect(exported(span)).not.toContain(PROMPT);
      }
    },
  );
});
