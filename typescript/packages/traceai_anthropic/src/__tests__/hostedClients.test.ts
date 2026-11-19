/**
 * The cloud-hosted Anthropic clients (@anthropic-ai/bedrock-sdk, @anthropic-ai/vertex-sdk) against the real
 * SDKs, with a synthetic fetch. No network, no credentials.
 */
import { describe, it, expect, beforeEach, afterEach } from "@jest/globals";
import { crc32 } from "node:zlib";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import * as bedrockModule from "@anthropic-ai/bedrock-sdk";
import * as vertexModule from "@anthropic-ai/vertex-sdk";
import { AnthropicBedrock } from "@anthropic-ai/bedrock-sdk";
import { AnthropicVertex } from "@anthropic-ai/vertex-sdk";
import { AnthropicInstrumentation } from "../instrumentation";

const PROMPT = "PROMPT_CANARY_1f3a";
const ANSWER = "ANSWER_CANARY_9c2b";
const ECHO = "REQUEST_ECHO_CANARY_77d0";
const REQUEST_ARGS = { count: 4242, ratio: 0.4244, flag: true, label: "REQUEST_ARG_CANARY_5e51" };
const RESPONSE_ARGS = { count: 5353, ratio: 0.5354, flag: false, label: "RESPONSE_ARG_CANARY_8a07" };

const message = {
  id: "msg_synthetic",
  type: "message",
  role: "assistant",
  model: "synthetic-model",
  content: [
    { type: "text", text: ANSWER },
    { type: "tool_use", id: "toolu_1", name: "lookup", input: RESPONSE_ARGS },
  ],
  stop_reason: "tool_use",
  stop_sequence: null,
  usage: { input_tokens: 7, output_tokens: 3 },
  // A field outside the response-side set that echoes the request.
  request_echo: ECHO,
};

const streamEvents = [
  { type: "message_start", message: { ...message, content: [], stop_reason: null, usage: { input_tokens: 7, output_tokens: 0 } } },
  { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
  { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "ANSWER_CANARY_" } },
  { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "9c2b" } },
  { type: "content_block_stop", index: 0 },
  { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 3 } },
  { type: "message_stop" },
];

const history = [
  { role: "user" as const, content: PROMPT },
  { role: "assistant" as const, content: [{ type: "tool_use" as const, id: "toolu_0", name: "lookup", input: REQUEST_ARGS }] },
  { role: "user" as const, content: [{ type: "tool_result" as const, tool_use_id: "toolu_0", content: "ok" }] },
];

// --- AWS event stream framing (what the Bedrock SDK decodes) ---------------------------------
function header(name: string, value: string): Buffer {
  const n = Buffer.from(name);
  const v = Buffer.from(value);
  return Buffer.concat([Buffer.from([n.length]), n, Buffer.from([7]), Buffer.from([v.length >> 8, v.length & 255]), v]);
}
function frame(payload: Buffer): Buffer {
  const headers = Buffer.concat([header(":event-type", "chunk"), header(":content-type", "application/json"), header(":message-type", "event")]);
  const total = 16 + headers.length + payload.length;
  const prelude = Buffer.alloc(8);
  prelude.writeUInt32BE(total, 0);
  prelude.writeUInt32BE(headers.length, 4);
  const preludeCrc = Buffer.alloc(4);
  preludeCrc.writeUInt32BE(crc32(prelude), 0);
  const body = Buffer.concat([prelude, preludeCrc, headers, payload]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body), 0);
  return Buffer.concat([body, crc]);
}
function awsChunk(event: unknown): Buffer {
  const bytes = Buffer.from(JSON.stringify(event)).toString("base64");
  return frame(Buffer.from(JSON.stringify({ bytes })));
}

function bodyStream(chunks: Uint8Array[], perChunkDelay = 2): ReadableStream<Uint8Array> {
  return new ReadableStream({
    async start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(chunk);
        await new Promise((resolve) => setTimeout(resolve, perChunkDelay));
      }
      controller.close();
    },
  });
}

type Flavor = "bedrock" | "vertex";
const urls: string[] = [];
const syntheticFetch = (flavor: Flavor) => async (url: unknown, init: any) => {
  urls.push(String(url));
  const failing = String(url).includes("synthetic-error");
  if (failing) {
    return new Response(JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "synthetic failure" } }), {
      status: 400,
      headers: { "content-type": "application/json", "request-id": "req_synthetic" },
    });
  }
  const streaming = String(url).includes("invoke-with-response-stream") || String(url).includes("streamRawPredict");
  if (!streaming) {
    return new Response(JSON.stringify(message), { status: 200, headers: { "content-type": "application/json", "request-id": "req_synthetic" } });
  }
  if (flavor === "bedrock") {
    return new Response(bodyStream(streamEvents.map((event) => new Uint8Array(awsChunk(event)))), {
      status: 200,
      headers: { "content-type": "application/vnd.amazon.eventstream", "request-id": "req_synthetic" },
    });
  }
  const encoder = new TextEncoder();
  return new Response(bodyStream(streamEvents.map((e) => encoder.encode(`event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`))), {
    status: 200,
    headers: { "content-type": "text/event-stream", "request-id": "req_synthetic" },
  });
};

function bedrockClient() {
  return new AnthropicBedrock({
    awsRegion: "us-east-1", awsAccessKey: "AKIASYNTHETIC", awsSecretKey: "synthetic", maxRetries: 0,
    fetch: syntheticFetch("bedrock") as any,
  });
}
function vertexClient() {
  return new AnthropicVertex({
    region: "us-east5", projectId: "synthetic-project", maxRetries: 0,
    googleAuth: { getClient: async () => ({ getRequestHeaders: async () => ({ authorization: "Bearer synthetic" }), projectId: "synthetic-project" }) } as any,
    fetch: syntheticFetch("vertex") as any,
  });
}

const CASES: Array<{ flavor: Flavor; provider: string; model: string; make: () => { messages: { create: (...args: any[]) => any } } }> = [
  { flavor: "bedrock", provider: "aws.bedrock", model: "anthropic.claude-3-sonnet-20240229-v1:0", make: bedrockClient as any },
  { flavor: "vertex", provider: "gcp.vertex_ai", model: "claude-3-sonnet@20240229", make: vertexClient as any },
];

let memory: InMemorySpanExporter;
let provider: BasicTracerProvider;
let instrumentation: AnthropicInstrumentation;

function setup(traceConfig?: Record<string, boolean>) {
  memory = new InMemorySpanExporter();
  provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  instrumentation.manuallyInstrument(bedrockModule);
  instrumentation.manuallyInstrument(vertexModule);
}
async function finished(count = 1) {
  for (let i = 0; i < 100 && memory.getFinishedSpans().length < count; i++) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  await new Promise((resolve) => setTimeout(resolve, 20));
  return memory.getFinishedSpans();
}
afterEach(async () => {
  instrumentation.disable();
  await provider.shutdown();
});

describe.each(CASES)("$flavor client", ({ provider: providerName, model, make }) => {
  beforeEach(() => setup());

  it("traces messages.create with the provider and model id", async () => {
    const result = await make().messages.create({ model, max_tokens: 16, messages: history });
    expect(result.id).toBe("msg_synthetic");
    const spans = await finished();
    expect(spans).toHaveLength(1);
    const a = spans[0].attributes;
    expect(a["fi.span.kind"]).toBe("LLM");
    expect(a["gen_ai.provider.name"]).toBe(providerName);
    expect(a["gen_ai.request.model"]).toBe(model);
    expect(a["gen_ai.usage.input_tokens"]).toBe(7);
    expect(a["gen_ai.usage.output_tokens"]).toBe(3);
    expect(JSON.stringify(a)).toContain(ANSWER);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("ends one span for a streamed call, also when the consumer stops early", async () => {
    const stream = await make().messages.create({ model, max_tokens: 16, messages: history, stream: true });
    let text = "";
    for await (const event of stream as AsyncIterable<any>) {
      if (event.type === "content_block_delta") text += event.delta.text;
    }
    expect(text).toBe(ANSWER);
    let spans = await finished();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["gen_ai.provider.name"]).toBe(providerName);
    expect(JSON.stringify(spans[0].attributes)).toContain(ANSWER);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);

    memory.reset();
    const early = await make().messages.create({ model, max_tokens: 16, messages: history, stream: true });
    for await (const event of early as AsyncIterable<any>) {
      if (event.type === "content_block_delta") break;
    }
    spans = await finished();
    expect(spans).toHaveLength(1);
  });

  it("ends one ERROR span when the request fails", async () => {
    await expect(make().messages.create({ model: `${model}-synthetic-error`, max_tokens: 16, messages: history })).rejects.toThrow();
    const spans = await finished();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("records no span after the instrumentation is disabled", async () => {
    instrumentation.disable();
    await make().messages.create({ model, max_tokens: 16, messages: history });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(memory.getFinishedSpans()).toHaveLength(0);
  });
});

describe.each(CASES)("$flavor client privacy", ({ model, make }) => {
  const run = async (traceConfig?: Record<string, boolean>, stream = false) => {
    await afterEachNow();
    setup(traceConfig);
    const call = make().messages.create({ model, max_tokens: 16, messages: history, ...(stream ? { stream: true } : {}) });
    const result: any = await call;
    if (stream) for await (const _ of result as AsyncIterable<any>) { /* consume */ }
    const spans = await finished();
    expect(spans).toHaveLength(1);
    return JSON.stringify(spans[0].attributes) + JSON.stringify(spans[0].events);
  };
  const afterEachNow = async () => {
    try { instrumentation?.disable(); await provider?.shutdown(); } catch { /* first run */ }
  };

  it("records prompt, answer, tool arguments and the raw objects with no hide setting", async () => {
    const dumped = await run();
    for (const canary of [PROMPT, ANSWER, "REQUEST_ARG_CANARY_5e51", "RESPONSE_ARG_CANARY_8a07", "4242", "0.4244", "5353", "0.5354"]) expect(dumped).toContain(canary);
    expect(dumped).toContain("raw.input");
    expect(dumped).toContain("raw.output");
    expect(dumped).toContain(ECHO);
  });

  it.each([
    ["hideInputs", [PROMPT, "REQUEST_ARG_CANARY_5e51", "4242", "0.4244"]],
    ["hideInputMessages", [PROMPT, "REQUEST_ARG_CANARY_5e51"]],
    ["hideOutputs", [ANSWER, "RESPONSE_ARG_CANARY_8a07", "5353", "0.5354"]],
    ["hideOutputMessages", [ANSWER, "RESPONSE_ARG_CANARY_8a07"]],
  ])("%s keeps its content out of every attribute", async (flag, canaries) => {
    const dumped = await run({ [flag]: true });
    for (const canary of canaries) expect(dumped).not.toContain(canary);
    expect(dumped).toContain("gen_ai.usage.input_tokens");
  });

  it.each(["hideInputs", "hideOutputs", "hideInputMessages", "hideOutputMessages", "hideInputText", "hideOutputText"])(
    "%s: the raw request and response are not recorded",
    async (flag) => {
      const dumped = await run({ [flag]: true });
      expect(dumped).not.toContain("raw.input");
      expect(dumped).not.toContain("raw.output");
      expect(dumped).not.toContain(ECHO);
    },
  );

  it.each(["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"])(
    "%s: a streamed call records no whole-response object",
    async (flag) => {
      const dumped = await run({ [flag]: true }, true);
      expect(dumped).not.toContain("raw.output");
      expect(dumped).not.toContain(ECHO);
    },
  );

  it("an input hide setting records only response-side fields as output", async () => {
    const dumped = await run({ hideInputText: true });
    expect(dumped).not.toContain(ECHO);
    expect(dumped).toContain(ANSWER);
  });
});
