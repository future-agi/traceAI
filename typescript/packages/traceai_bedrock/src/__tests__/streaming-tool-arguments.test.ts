import { describe, it, expect } from "@jest/globals";
import { InvokeModelWithResponseStreamCommand } from "@aws-sdk/client-bedrock-runtime";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { BedrockInstrumentation } from "../instrumentation";

function toolStart(
  index: number,
  id = "toolu_demo",
  name = "weather",
  input: Record<string, unknown> = {},
) {
  return {
    type: "content_block_start",
    index,
    content_block: { type: "tool_use", id, name, input },
  };
}

function jsonDelta(index: number, partial_json: string) {
  return {
    type: "content_block_delta",
    index,
    delta: { type: "input_json_delta", partial_json },
  };
}

function blockStop(index: number) {
  return { type: "content_block_stop", index };
}

/** Exercise the real send wrapper and stream splitter without an AWS request. */
async function captureStream(
  events: Record<string, unknown>[],
  streamError?: Error,
) {
  const chunks = events.map((event) =>
    Object.freeze({
      chunk: Object.freeze({
        bytes: new TextEncoder().encode(JSON.stringify(event)),
      }),
    }),
  );
  const originalBytes = chunks.map(({ chunk }) => Array.from(chunk.bytes));
  async function* body() {
    for (const chunk of chunks) {
      yield chunk;
      // Let both split streams consume each event before a possible error.
      await new Promise<void>((resolve) => setImmediate(resolve));
    }
    if (streamError) throw streamError;
  }
  class BedrockRuntimeClient {
    async send(_command: unknown) {
      return { body: body(), contentType: "application/json" };
    }
  }

  const exporter = new InMemorySpanExporter();
  let finish!: () => void;
  const finished = new Promise<void>((resolve) => { finish = resolve; });
  class NotifyingSpanProcessor extends SimpleSpanProcessor {
    onEnd(span: ReadableSpan): void {
      super.onEnd(span);
      finish();
    }
  }
  const provider = new BasicTracerProvider({
    spanProcessors: [new NotifyingSpanProcessor(exporter)],
  });
  const instrumentation = new BedrockInstrumentation({
    tracerProvider: provider,
    instrumentationConfig: { enabled: false },
  });
  instrumentation.manuallyInstrument({
    BedrockRuntimeClient,
  } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);

  try {
    const response = await new BedrockRuntimeClient().send(
      new InvokeModelWithResponseStreamCommand({
        modelId: "anthropic.claude-3-haiku-20240307-v1:0",
        body: JSON.stringify({
          messages: [{ role: "user", content: "What is the weather?" }],
        }),
      }),
    );
    const received: unknown[] = [];
    let error: unknown;
    try {
      for await (const chunk of response.body) received.push(chunk);
    } catch (caught) {
      error = caught;
    }
    await finished;
    await provider.forceFlush();

    expect(response.contentType).toBe("application/json");
    expect(received).toHaveLength(chunks.length);
    received.forEach((chunk, index) => expect(chunk).toBe(chunks[index]));
    expect(chunks.map(({ chunk }) => Array.from(chunk.bytes))).toEqual(originalBytes);
    expect(error).toBe(streamError);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    return { span: spans[0], error };
  } finally {
    instrumentation.disable();
    await provider.shutdown();
  }
}

function outputMessage(span: ReadableSpan) {
  return JSON.parse(span.attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string)[0];
}

function outputValue(span: ReadableSpan) {
  return JSON.parse(span.attributes[SemanticConventions.OUTPUT_VALUE] as string);
}

function toolCall(id: string, name: string, args: string) {
  return { id, function: { name, arguments: args } };
}

describe("T8-TS-CLOUD-005 Bedrock streamed tool arguments", () => {
  it("exports JSON deltas from the instrumented response", async () => {
    const args = JSON.stringify({ city: "Paris" });
    const { span } = await captureStream([
      toolStart(0),
      jsonDelta(0, args),
      blockStop(0),
    ]);

    expect(outputMessage(span)).toEqual({
      role: "assistant",
      tool_calls: [toolCall("toolu_demo", "weather", args)],
    });
    expect(outputValue(span).tool_calls).toEqual([
      { id: "toolu_demo", name: "weather", input: { city: "Paris" } },
    ]);
    expect(span.status.code).toBe(SpanStatusCode.OK);
  });

  it("joins fragments by block index without losing text or usage", async () => {
    const args = JSON.stringify({ city: 'Par"is', units: "°C" });
    const split = args.indexOf("\\") + 1;
    const { span } = await captureStream([
      { type: "message_start", message: { usage: { input_tokens: 5 } } },
      { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
      { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Checking " } },
      toolStart(1),
      toolStart(7, "toolu_time", "time"),
      jsonDelta(99, '{"ignored":true}'),
      jsonDelta(0, '{"ignored":true}'),
      jsonDelta(1, args.slice(0, split)),
      jsonDelta(7, '{"zone":'),
      jsonDelta(1, args.slice(split)),
      jsonDelta(7, '"UTC"}'),
      blockStop(7),
      blockStop(1),
      { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "weather." } },
      blockStop(0),
      { type: "message_delta", usage: { output_tokens: 9 } },
    ]);

    expect(outputMessage(span)).toEqual({
      role: "assistant",
      content: "Checking weather.",
      tool_calls: [
        toolCall("toolu_demo", "weather", args),
        toolCall("toolu_time", "time", '{"zone":"UTC"}'),
      ],
    });
    expect(outputValue(span).tool_calls.map((tool: { input: unknown }) => tool.input)).toEqual([
      { city: 'Par"is', units: "°C" },
      { zone: "UTC" },
    ]);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBe(9);
  });

  it("preserves initial tool input when no JSON delta arrives", async () => {
    const { span } = await captureStream([
      toolStart(0, "toolu_demo", "weather", { city: "Paris" }),
      blockStop(0),
    ]);
    expect(outputMessage(span).tool_calls).toEqual([
      toolCall("toolu_demo", "weather", '{"city":"Paris"}'),
    ]);
    expect(outputValue(span).tool_calls[0].input).toEqual({ city: "Paris" });
  });

  it.each([
    { label: "complete JSON without a block stop", args: '{"city":"Paris"}', input: { city: "Paris" }, stop: false, incomplete: false },
    { label: "partial JSON without a block stop", args: '{"city":"Par', input: "<omitted: incomplete tool arguments>", stop: false, incomplete: true },
    { label: "malformed JSON at a block stop", args: '{"city":oops}', input: "<omitted: incomplete tool arguments>", stop: true, incomplete: true },
  ])("preserves $label", async ({ args, input, stop, incomplete }) => {
    const { span } = await captureStream([
      toolStart(0),
      jsonDelta(0, args),
      ...(stop ? [blockStop(0)] : []),
    ]);
    expect(outputMessage(span).tool_calls).toEqual([
      toolCall("toolu_demo", "weather", incomplete ? "<omitted: incomplete tool arguments>" : args),
    ]);
    expect(outputValue(span).tool_calls[0].input).toEqual(input);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    if (incomplete) {
      expect(span.attributes["fi.tool_call.arguments_incomplete"]).toBe(true);
      expect(span.attributes["fi.tool_call.arguments_received_length"]).toBe(args.length);
      expect(JSON.stringify(span.attributes)).not.toContain(JSON.stringify(args).slice(1, -1));
    }
  });

  it("retains partial arguments on stream failure and preserves the application error", async () => {
    const failure = new Error("offline stream interrupted");
    const args = '{"city":"Par';
    const { span, error } = await captureStream([
      toolStart(0),
      jsonDelta(0, args),
    ], failure);

    expect(error).toBe(failure);
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(outputMessage(span).tool_calls).toEqual([
      toolCall("toolu_demo", "weather", "<omitted: incomplete tool arguments>"),
    ]);
    expect(outputValue(span).tool_calls[0].input).toBe("<omitted: incomplete tool arguments>");
    expect(span.attributes["fi.tool_call.arguments_incomplete"]).toBe(true);
    expect(span.attributes["fi.tool_call.arguments_received_length"]).toBe(args.length);
  });
});
