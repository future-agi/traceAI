/**
 * Content and error-detail hiding with the real OpenAI SDK against a local HTTP server.
 *
 * The instrumentation passes the prompt to FITracer.startSpan as creation-time attributes
 * (input.value, gen_ai.input.messages, raw.input), so this checks that those go through the
 * same masking as attributes set later, and that a provider error quoting the prompt is hidden.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "@jest/globals";
import http from "http";
import { AddressInfo } from "net";
import OpenAI from "openai";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { FITracer, REDACTED_VALUE, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { OpenAIInstrumentation } from "../instrumentation";

const PROMPT = "my secret prompt: the launch code is 0000";
const SYSTEM_PROMPT = "system secret: never say 4242";
const MISSING_MODEL = "missing-model";

/** When true, the server's answers quote the prompt (to check output hiding too) */
let echoPrompt = false;

function answer(): string {
  return echoPrompt ? `You said: ${PROMPT}` : "Sure.";
}

function handle(req: http.IncomingMessage, res: http.ServerResponse, raw: string) {
  const body = JSON.parse(raw || "{}");
  res.setHeader("x-request-id", "req_123");
  if (body.model === MISSING_MODEL) {
    // Like OpenAI-compatible providers, the error body quotes the request
    res.statusCode = 404;
    res.setHeader("content-type", "application/json");
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
  if (req.url?.endsWith("/chat/completions") && body.stream) {
    res.setHeader("content-type", "text/event-stream");
    const chunk = (delta: object, finish: string | null) =>
      `data: ${JSON.stringify({
        id: "chatcmpl-stream",
        object: "chat.completion.chunk",
        created: 1,
        model: body.model,
        choices: [{ index: 0, delta, finish_reason: finish }],
      })}\n\n`;
    res.write(chunk({ role: "assistant", content: answer() }, null));
    res.write(chunk({}, "stop"));
    res.end("data: [DONE]\n\n");
    return;
  }
  res.setHeader("content-type", "application/json");
  const usage = { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 };
  if (req.url?.endsWith("/chat/completions")) {
    res.end(
      JSON.stringify({
        id: "chatcmpl-1",
        object: "chat.completion",
        created: 1,
        model: body.model,
        choices: [
          { index: 0, message: { role: "assistant", content: answer() }, finish_reason: "stop" },
        ],
        usage,
      }),
    );
    return;
  }
  res.end(
    JSON.stringify({
      id: "cmpl-1",
      object: "text_completion",
      created: 1,
      model: body.model,
      choices: [{ index: 0, text: answer(), logprobs: null, finish_reason: "stop" }],
      usage,
    }),
  );
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

describe("OpenAI instrumentation with hidden content", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const instrumentation = new OpenAIInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(OpenAI);
  let server: http.Server;
  let client: OpenAI;

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      let raw = "";
      req.on("data", (part) => (raw += part));
      req.on("end", () => handle(req, res, raw));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    client = new OpenAI({
      apiKey: "test-key",
      baseURL: `http://127.0.0.1:${port}/v1`,
      maxRetries: 0,
    });
  });
  afterAll(async () => {
    await new Promise((resolve) => server.close(resolve));
  });
  beforeEach(() => {
    exporter.reset();
    echoPrompt = false;
  });

  /** The patch reads the instrumentation's FITracer on every call */
  function useTraceConfig(traceConfig: TraceConfigOptions) {
    (instrumentation as unknown as { fiTracer: FITracer }).fiTracer = new FITracer({
      tracer: provider.getTracer("test"),
      traceConfig,
    });
  }

  const messages = [
    { role: "system" as const, content: SYSTEM_PROMPT },
    { role: "user" as const, content: PROMPT },
  ];

  /** One of each call shape: success, provider error, stream; chat and legacy completions */
  async function callEveryShape() {
    await client.chat.completions.create({ model: "gpt-4o-mini", messages });
    await expect(
      client.chat.completions.create({ model: MISSING_MODEL, messages }),
    ).rejects.toThrow("does not exist");
    const stream = await client.chat.completions.create({
      model: "gpt-4o-mini",
      messages,
      stream: true,
    });
    for await (const _chunk of stream) {
      // drain
    }
    await client.completions.create({ model: "gpt-3.5-turbo-instruct", prompt: PROMPT });
    await expect(
      client.completions.create({ model: MISSING_MODEL, prompt: PROMPT }),
    ).rejects.toThrow("does not exist");
    // The stream span ends after the stream is consumed
    await new Promise((resolve) => setTimeout(resolve, 50));
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(5);
    return spans;
  }

  it("control: without hide flags the prompt is exported (the check can see it)", async () => {
    useTraceConfig({});
    const spans = await callEveryShape();
    for (const span of spans) {
      expect(exported(span)).toContain(PROMPT);
    }
  });

  it("hideInputs: the prompt is in no exported attribute, event or status", async () => {
    useTraceConfig({ hideInputs: true });
    const spans = await callEveryShape();
    for (const span of spans) {
      expect(exported(span)).not.toContain(PROMPT);
      expect(exported(span)).not.toContain(SYSTEM_PROMPT);
      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe(REDACTED_VALUE);
      expect(span.attributes[SemanticConventions.RAW_INPUT]).toBe(REDACTED_VALUE);
      expect(span.attributes[SemanticConventions.LLM_INPUT_MESSAGES]).toBeUndefined();
      // Not content: kept
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBeDefined();
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe("openai");
    }
    const failed = spans.filter((span) => span.status.code === SpanStatusCode.ERROR);
    expect(failed).toHaveLength(2);
    for (const span of failed) {
      expect(span.status).toEqual({ code: SpanStatusCode.ERROR });
      expect(span.attributes["error.type"]).toBe("model_not_found");
      const events = span.events.filter((event) => event.name === "exception");
      expect(events).toHaveLength(1);
      expect(events[0].attributes).toEqual({ "exception.type": "model_not_found" });
    }
    // Outputs are not hidden by hideInputs
    const ok = spans.filter((span) => span.status.code !== SpanStatusCode.ERROR);
    for (const span of ok) {
      expect(String(span.attributes[SemanticConventions.OUTPUT_VALUE])).toContain("Sure.");
    }
  });

  it("hideInputs + hideOutputs: an answer that quotes the prompt is hidden too", async () => {
    useTraceConfig({ hideInputs: true, hideOutputs: true });
    echoPrompt = true;
    const spans = await callEveryShape();
    for (const span of spans) {
      expect(exported(span)).not.toContain(PROMPT);
      expect(exported(span)).not.toContain(SYSTEM_PROMPT);
    }
    const ok = spans.filter((span) => span.status.code !== SpanStatusCode.ERROR);
    for (const span of ok) {
      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(REDACTED_VALUE);
      expect(span.attributes[SemanticConventions.RAW_OUTPUT]).toBe(REDACTED_VALUE);
      expect(span.attributes[SemanticConventions.LLM_OUTPUT_MESSAGES]).toBeUndefined();
    }
  });

  it("default mode keeps the provider message only, with error.type", async () => {
    useTraceConfig({});
    await expect(
      client.chat.completions.create({ model: MISSING_MODEL, messages }),
    ).rejects.toThrow();
    const [span] = exporter.getFinishedSpans();
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(span.status.message).toContain("does not exist");
    expect(span.status.message).not.toContain("\n");
    expect(span.attributes["error.type"]).toBe("model_not_found");
  });
});
