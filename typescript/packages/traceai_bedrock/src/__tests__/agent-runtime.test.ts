import { describe, it, expect, afterEach } from "@jest/globals";
import {
  BedrockAgentRuntimeClient,
  InvokeAgentCommand,
  InvokeInlineAgentCommand,
} from "@aws-sdk/client-bedrock-agent-runtime";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { SpanStatusCode } from "@opentelemetry/api";
import type { TraceConfigOptions } from "@traceai/fi-core";
import { BedrockInstrumentation } from "../instrumentation";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

type Event = Record<string, unknown>;

const chunk = (text: string): Event => ({ chunk: { bytes: encoder.encode(text) } });
const trace = (part: Event): Event => ({
  trace: { agentId: "AGENT1", agentAliasId: "ALIAS1", sessionId: "session-1", trace: part },
});

/** A completion stream that records whether the consumer closed it early. */
function completionStream(events: Event[], opts: { failWith?: Error } = {}) {
  const state = { closed: false, pulled: 0 };
  async function* generate() {
    try {
      for (const event of events) {
        state.pulled++;
        yield event;
      }
      if (opts.failWith) throw opts.failWith;
    } finally {
      state.closed = true;
    }
  }
  return { stream: generate(), state };
}

/**
 * The real SDK client with its network stack replaced by a middleware that answers with a
 * recorded event stream. No credentials, no network.
 */
function setup(
  respond: () => unknown,
  traceConfig?: TraceConfigOptions,
) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const instrumentation = new BedrockInstrumentation({
    instrumentationConfig: { enabled: false },
    tracerProvider: provider,
    traceConfig,
  });
  instrumentation.manuallyInstrument({
    BedrockAgentRuntimeClient,
  } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
  const client = new BedrockAgentRuntimeClient({
    region: "us-east-1",
    credentials: { accessKeyId: "test", secretAccessKey: "test" },
  });
  client.middlewareStack.add(
    () => async () => {
      const output = respond();
      return { output, response: {} } as never;
    },
    { step: "initialize", name: "recordedAgentResponse", priority: "low" },
  );
  return { client, exporter, instrumentation };
}

const command = (extra: Record<string, unknown> = {}) =>
  new InvokeAgentCommand({
    agentId: "AGENT1",
    agentAliasId: "ALIAS1",
    sessionId: "session-1",
    inputText: "What is the weather in Paris?",
    ...extra,
  });

async function drain(completion: AsyncIterable<Event>): Promise<Event[]> {
  const out: Event[] = [];
  for await (const event of completion) out.push(event);
  return out;
}

const attr = (span: ReadableSpan, key: string) => span.attributes[key];
const byName = (spans: ReadableSpan[], name: string) => spans.find((s) => s.name === name);
const parentId = (span: ReadableSpan) => span.parentSpanContext?.spanId;

const tracedEvents = (): Event[] => [
  trace({
    orchestrationTrace: {
      modelInvocationInput: {
        traceId: "t-1-a-b-c-0",
        type: "ORCHESTRATION",
        foundationModel: "anthropic.claude-3-sonnet-20240229-v1:0",
        inferenceConfiguration: { temperature: 0.2, maximumLength: 2048 },
        text: JSON.stringify({
          system: "You are a weather agent.",
          messages: [{ role: "user", content: "What is the weather in Paris?" }],
        }),
      },
    },
  }),
  trace({
    orchestrationTrace: {
      modelInvocationOutput: {
        traceId: "t-1-a-b-c-0",
        parsedResponse: { text: "I will call the weather tool" },
        metadata: { usage: { inputTokens: 120, outputTokens: 30 } },
      },
    },
  }),
  trace({
    orchestrationTrace: {
      invocationInput: {
        traceId: "t-1-a-b-c-1",
        invocationType: "ACTION_GROUP",
        actionGroupInvocationInput: {
          actionGroupName: "weather",
          function: "get_weather",
          executionType: "LAMBDA",
          parameters: [{ name: "city", type: "string", value: "Paris" }],
        },
      },
    },
  }),
  trace({
    orchestrationTrace: {
      observation: {
        traceId: "t-1-a-b-c-1",
        type: "ACTION_GROUP",
        actionGroupInvocationOutput: { text: "21C and sunny" },
      },
    },
  }),
  trace({
    orchestrationTrace: {
      invocationInput: {
        traceId: "t-1-a-b-c-2",
        invocationType: "KNOWLEDGE_BASE",
        knowledgeBaseLookupInput: { knowledgeBaseId: "KB123", text: "paris climate" },
      },
    },
  }),
  trace({
    orchestrationTrace: {
      observation: {
        traceId: "t-1-a-b-c-2",
        type: "KNOWLEDGE_BASE",
        knowledgeBaseLookupOutput: {
          retrievedReferences: [
            { content: { text: "PRIVATE DOC ONE" }, metadata: { "x-amz-bedrock-kb-chunk-id": "c1" } },
            { content: { text: "PRIVATE DOC TWO" }, metadata: { "x-amz-bedrock-kb-chunk-id": "c2" } },
          ],
        },
      },
    },
  }),
  chunk("It is "),
  chunk("21C in Paris."),
];

describe("Bedrock Agents (bedrock-agent-runtime) instrumentation", () => {
  afterEach(() => undefined);

  it("wraps a plain invoke in one AGENT span ended when the completion is consumed", async () => {
    const { stream } = completionStream([chunk("Hello "), chunk("there")]);
    const { client, exporter } = setup(() => ({ completion: stream, sessionId: "session-1" }));

    const response = await client.send(command());
    // Still streaming: the span stays open until the caller has read the stream.
    expect(exporter.getFinishedSpans()).toHaveLength(0);

    const events = await drain(response.completion as AsyncIterable<Event>);
    // The application sees the original events, unchanged.
    expect(events.map((e) => decoder.decode((e as { chunk: { bytes: Uint8Array } }).chunk.bytes)).join("")).toBe(
      "Hello there",
    );

    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    const span = spans[0];
    expect(span.name).toBe("invoke_agent AGENT1/ALIAS1");
    expect(attr(span, "fi.span.kind")).toBe("AGENT");
    expect(attr(span, "gen_ai.operation.name")).toBe("invoke_agent");
    expect(attr(span, "gen_ai.agent.id")).toBe("AGENT1");
    expect(attr(span, "session.id")).toBe("session-1");
    expect(attr(span, "input.value")).toBe("What is the weather in Paris?");
    expect(attr(span, "output.value")).toBe("Hello there");
    expect(span.status.code).toBe(SpanStatusCode.OK);
  });

  it("builds LLM, TOOL and RETRIEVER child spans from trace events when enableTrace is set", async () => {
    const { stream } = completionStream(tracedEvents());
    const { client, exporter } = setup(() => ({ completion: stream }));

    const response = await client.send(command({ enableTrace: true }));
    await drain(response.completion as AsyncIterable<Event>);

    const spans = exporter.getFinishedSpans();
    const agent = byName(spans, "invoke_agent AGENT1/ALIAS1")!;
    expect(agent).toBeDefined();
    expect(attr(agent, "output.value")).toBe("It is 21C in Paris.");

    const llm = spans.find((s) => attr(s, "fi.span.kind") === "LLM")!;
    expect(llm).toBeDefined();
    expect(parentId(llm)).toBe(agent.spanContext().spanId);
    expect(attr(llm, "gen_ai.request.model")).toBe("anthropic.claude-3-sonnet-20240229-v1:0");
    expect(attr(llm, "gen_ai.usage.input_tokens")).toBe(120);
    expect(attr(llm, "gen_ai.usage.output_tokens")).toBe(30);
    expect(String(attr(llm, "gen_ai.input.messages"))).toContain("What is the weather in Paris?");
    expect(String(attr(llm, "gen_ai.output.messages"))).toContain("I will call the weather tool");
    expect(llm.status.code).toBe(SpanStatusCode.OK);

    const tool = byName(spans, "execute_tool get_weather")!;
    expect(tool).toBeDefined();
    expect(attr(tool, "fi.span.kind")).toBe("TOOL");
    expect(attr(tool, "gen_ai.operation.name")).toBe("execute_tool");
    expect(attr(tool, "tool.name")).toBe("get_weather");
    expect(parentId(tool)).toBe(agent.spanContext().spanId);
    expect(String(attr(tool, "input.value"))).toContain("Paris");
    expect(attr(tool, "output.value")).toBe("21C and sunny");

    const retriever = spans.find((s) => attr(s, "fi.span.kind") === "RETRIEVER")!;
    expect(retriever).toBeDefined();
    expect(parentId(retriever)).toBe(agent.spanContext().spanId);
    expect(attr(retriever, "input.value")).toBe("paris climate");
    expect(JSON.parse(String(attr(retriever, "metadata")))).toMatchObject({
      knowledge_base_id: "KB123",
      document_count: 2,
    });
    expect(attr(retriever, "retrieval.documents.0.document.id")).toBe("c1");
    expect(attr(retriever, "retrieval.documents.1.document.id")).toBe("c2");
    expect(attr(retriever, "retrieval.documents.0.document.content")).toBe("PRIVATE DOC ONE");
    // Every span is ended: nothing leaks.
    expect(spans).toHaveLength(4);
  });

  it("traces InvokeInlineAgentCommand as an AGENT span with its foundation model", async () => {
    const { stream } = completionStream([
      trace({
        orchestrationTrace: {
          invocationInput: {
            traceId: "t-9-a-b-c-0",
            invocationType: "KNOWLEDGE_BASE",
            knowledgeBaseLookupInput: { knowledgeBaseId: "KB9", text: "q" },
          },
        },
      }),
      trace({
        orchestrationTrace: {
          observation: {
            traceId: "t-9-a-b-c-0",
            type: "KNOWLEDGE_BASE",
            knowledgeBaseLookupOutput: { retrievedReferences: [] },
          },
        },
      }),
      chunk("inline answer"),
    ]);
    const { client, exporter } = setup(() => ({ completion: stream }));
    const response = await client.send(
      new InvokeInlineAgentCommand({
        foundationModel: "anthropic.claude-3-haiku-20240307-v1:0",
        instruction: "Be helpful and concise when answering.",
        sessionId: "inline-session",
        inputText: "hello inline",
        enableTrace: true,
      }),
    );
    await drain(response.completion as AsyncIterable<Event>);

    const spans = exporter.getFinishedSpans();
    const agent = byName(spans, "invoke_agent inline")!;
    expect(agent).toBeDefined();
    expect(attr(agent, "fi.span.kind")).toBe("AGENT");
    expect(attr(agent, "gen_ai.operation.name")).toBe("invoke_agent");
    expect(attr(agent, "gen_ai.request.model")).toBe("anthropic.claude-3-haiku-20240307-v1:0");
    expect(attr(agent, "session.id")).toBe("inline-session");
    expect(attr(agent, "input.value")).toBe("hello inline");
    expect(attr(agent, "output.value")).toBe("inline answer");
    const retriever = spans.find((s) => attr(s, "fi.span.kind") === "RETRIEVER")!;
    expect(parentId(retriever)).toBe(agent.spanContext().spanId);
    expect(JSON.parse(String(attr(retriever, "metadata"))).document_count).toBe(0);
  });

  it("does not create child spans without trace events", async () => {
    const { stream } = completionStream([chunk("only text")]);
    const { client, exporter } = setup(() => ({ completion: stream }));
    const response = await client.send(command());
    await drain(response.completion as AsyncIterable<Event>);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("ends every span and closes the source stream when the caller stops reading early", async () => {
    const { stream, state } = completionStream(tracedEvents());
    const { client, exporter } = setup(() => ({ completion: stream }));
    const response = await client.send(command({ enableTrace: true }));

    let seen = 0;
    for await (const _event of response.completion as AsyncIterable<Event>) {
      if (++seen === 3) break; // model call finished, tool call still open
    }

    expect(state.closed).toBe(true);
    const spans = exporter.getFinishedSpans();
    const agent = byName(spans, "invoke_agent AGENT1/ALIAS1")!;
    expect(agent).toBeDefined();
    // The open TOOL span was ended too and is a child of the agent span.
    const tool = byName(spans, "execute_tool get_weather")!;
    expect(tool).toBeDefined();
    expect(parentId(tool)).toBe(agent.spanContext().spanId);
    expect(agent.events.map((e) => e.name)).toContain("stream.cancelled");
    expect(agent.status.code).not.toBe(SpanStatusCode.ERROR);
  });

  it("ends the span when the stream is abandoned before the first event", async () => {
    const { stream, state } = completionStream(tracedEvents());
    const { client, exporter } = setup(() => ({ completion: stream }));
    const response = await client.send(command());
    const iterator = (response.completion as AsyncIterable<Event>)[Symbol.asyncIterator]();
    await iterator.return?.();
    expect(state.closed).toBe(true);
    expect(exporter.getFinishedSpans().map((s) => s.name)).toEqual(["invoke_agent AGENT1/ALIAS1"]);
  });

  it("records ERROR with error.type when the stream fails, and rethrows the same error", async () => {
    const failure = Object.assign(new Error("throttled"), { name: "ThrottlingException" });
    const { stream } = completionStream([chunk("partial")], { failWith: failure });
    const { client, exporter } = setup(() => ({ completion: stream }));
    const response = await client.send(command());

    await expect(drain(response.completion as AsyncIterable<Event>)).rejects.toBe(failure);

    const span = exporter.getFinishedSpans()[0];
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(attr(span, "error.type")).toBe("ThrottlingException");
    expect(span.events.some((e) => e.name === "exception")).toBe(true);
  });

  it("records ERROR with error.type when send itself rejects, and rethrows unchanged", async () => {
    const failure = Object.assign(new Error("denied"), { name: "AccessDeniedException" });
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new BedrockInstrumentation({
      instrumentationConfig: { enabled: false },
      tracerProvider: provider,
    });
    instrumentation.manuallyInstrument({
      BedrockAgentRuntimeClient,
    } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
    const client = new BedrockAgentRuntimeClient({
      region: "us-east-1",
      credentials: { accessKeyId: "test", secretAccessKey: "test" },
    });
    client.middlewareStack.add(() => async () => { throw failure; }, {
      step: "initialize",
      name: "fail",
      priority: "low",
    });

    await expect(client.send(command())).rejects.toBe(failure);

    const span = exporter.getFinishedSpans()[0];
    expect(span.name).toBe("invoke_agent AGENT1/ALIAS1");
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(attr(span, "error.type")).toBe("AccessDeniedException");
  });

  it("does not trace other agent-runtime commands", async () => {
    class RetrieveCommand {}
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new BedrockInstrumentation({
      instrumentationConfig: { enabled: false },
      tracerProvider: provider,
    });
    const calls: unknown[] = [];
    class Fake {
      send(c: unknown) {
        calls.push(c);
        return Promise.resolve({ ok: true });
      }
    }
    instrumentation.manuallyInstrument({
      BedrockAgentRuntimeClient: Fake,
    } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
    const c = new RetrieveCommand();
    await expect(new Fake().send(c)).resolves.toEqual({ ok: true });
    expect(calls).toEqual([c]);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("hides prompts, outputs and document text under the hide settings", async () => {
    const { stream } = completionStream(tracedEvents());
    const { client, exporter } = setup(() => ({ completion: stream }), {
      hideInputs: true,
      hideOutputs: true,
    });
    const response = await client.send(command({ enableTrace: true }));
    await drain(response.completion as AsyncIterable<Event>);

    const spans = exporter.getFinishedSpans();
    expect(spans.length).toBe(4);
    const dump = JSON.stringify(spans.map((s) => s.attributes));
    for (const secret of [
      "What is the weather in Paris?",
      "It is 21C in Paris.",
      "PRIVATE DOC ONE",
      "PRIVATE DOC TWO",
      "21C and sunny",
      "paris climate",
      "I will call the weather tool",
    ]) {
      expect(dump).not.toContain(secret);
    }
    // Structure stays visible.
    expect(byName(spans, "invoke_agent AGENT1/ALIAS1")).toBeDefined();
    expect(byName(spans, "execute_tool get_weather")).toBeDefined();
  });
});
