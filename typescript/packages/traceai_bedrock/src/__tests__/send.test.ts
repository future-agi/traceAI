import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import {
  BedrockRuntimeClient,
  ConverseCommand,
  ConverseStreamCommand,
  InvokeModelCommand,
  InvokeModelWithResponseStreamCommand,
} from "@aws-sdk/client-bedrock-runtime";
import { BedrockInstrumentation } from "../instrumentation";

const input = { modelId: "amazon.nova-lite-v1:0", messages: [{ role: "user" as const, content: [{ text: "Hello" }] }] };
const invokeInput = { modelId: "amazon.titan-text-express-v1", body: JSON.stringify({ inputText: "Hello" }) };
const converseOutput = { output: { message: { role: "assistant", content: [{ text: "Hi" }] } }, usage: { inputTokens: 2, outputTokens: 1, totalTokens: 3 } };
const invokeOutput = { body: new TextEncoder().encode(JSON.stringify({ results: [{ outputText: "Hi", tokenCount: 1 }], inputTextTokenCount: 2 })) };

// Patch the real SDK prototype, replacing only transport. No credentials or network.
describe("Bedrock send overloads and streams", () => {
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let instrumentation: BedrockInstrumentation;
  let original: jest.Mock;
  let client: BedrockRuntimeClient;
  const descriptor = Object.getOwnPropertyDescriptor(BedrockRuntimeClient.prototype, "send");

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    original = jest.fn();
    Object.defineProperty(BedrockRuntimeClient.prototype, "send", { value: original, configurable: true, writable: true });
    instrumentation = new BedrockInstrumentation({ instrumentationConfig: { enabled: false }, tracerProvider: provider });
    instrumentation.manuallyInstrument({ BedrockRuntimeClient });
    client = new BedrockRuntimeClient({ region: "us-east-1", credentials: { accessKeyId: "synthetic", secretAccessKey: "synthetic" } });
  });

  afterEach(async () => {
    instrumentation.disable();
    if (descriptor) Object.defineProperty(BedrockRuntimeClient.prototype, "send", descriptor);
    else delete (BedrockRuntimeClient.prototype as Partial<BedrockRuntimeClient>).send;
    client.destroy();
    await provider.shutdown();
  });

  const commands = [
    ["InvokeModel", () => new InvokeModelCommand(invokeInput), () => invokeOutput],
    ["Converse", () => new ConverseCommand(input), () => converseOutput],
    ["InvokeModelWithResponseStream", () => new InvokeModelWithResponseStreamCommand(invokeInput), () => ({ body: (async function* () {})() })],
    ["ConverseStream", () => new ConverseStreamCommand(input), () => ({ stream: (async function* () {})() })],
  ] as const;

  async function consume(response: any) {
    const stream = response.stream ?? response.body;
    if (stream?.[Symbol.asyncIterator]) for await (const _event of stream) { /* consume */ }
  }

  it.each(commands)("%s forwards options and preserves the response", async (_name, command, output) => {
    const options = { abortSignal: new AbortController().signal, requestTimeout: 50 };
    const response = output();
    original.mockResolvedValue(response);
    const cmd = command();
    const result = await client.send(cmd as any, options);
    expect(original.mock.calls[0]).toEqual([cmd, options]);
    expect(original.mock.contexts[0]).toBe(client);
    await consume(result);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it.each(commands)("%s supports both callback overloads without returning a Promise", async (_name, command, output) => {
    for (const withOptions of [false, true]) {
      exporter.reset();
      const response = output();
      let pending: Function;
      const receiver = {};
      original.mockImplementation((...args) => { pending = args[args.length - 1]; return undefined; });
      const callback = jest.fn(function (this: unknown, error, data, extra) {
        expect(this).toBe(receiver);
        expect(error).toBeNull();
        expect(extra).toBe("extra");
        return data;
      });
      const cmd = command();
      const options = { requestTimeout: 50 };
      const args = withOptions ? [cmd, options, callback] : [cmd, callback];
      expect((client.send as Function)(...args)).toBeUndefined();
      expect(original.mock.lastCall).toHaveLength(args.length);
      if (withOptions) expect(original.mock.lastCall![1]).toBe(options);
      const result = pending!.call(receiver, null, response, "extra");
      expect(callback).toHaveBeenCalledTimes(1);
      await consume(result);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    }
  });

  it("passes unsupported commands and their callbacks through untouched", () => {
    // A command-shaped object works across SDK versions predating CountTokens.
    const command = { constructor: { name: "CountTokensCommand" } };
    const options = { abortSignal: new AbortController().signal };
    const callback = jest.fn();
    const sentinel = {};
    original.mockReturnValue(sentinel);
    expect((client.send as Function)(command, options, callback)).toBe(sentinel);
    expect(original).toHaveBeenCalledWith(command, options, callback);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it.each(commands)("%s records synchronous transport errors exactly once", (_name, command) => {
    const error = new Error("transport failed");
    original.mockImplementation(() => { throw error; });
    expect(() => client.send(command() as any)).toThrow(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it.each(commands)("%s preserves rejected abort errors and the original signal", async (_name, command) => {
    const controller = new AbortController();
    const error = Object.assign(new Error("aborted"), { name: "AbortError" });
    original.mockImplementation((_cmd, options) => new Promise((_resolve, reject) => {
      options.abortSignal.addEventListener("abort", () => reject(error), { once: true });
    }));
    const result = client.send(command() as any, { abortSignal: controller.signal });
    controller.abort();
    await expect(result).rejects.toBe(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it.each(commands)("%s forwards callback errors without changing synchronous return values", (_name, command) => {
    const error = new Error("callback transport failed");
    const sentinel = {};
    original.mockImplementation((_command, callback) => { callback(error); return sentinel; });
    const callback = jest.fn();
    expect((client.send as Function)(command(), callback)).toBe(sentinel);
    expect(callback).toHaveBeenCalledWith(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("does not swallow or reinterpret a synchronous user callback exception", () => {
    const error = new Error("application callback");
    original.mockImplementation((_cmd, callback) => callback(null, converseOutput));
    expect(() => client.send(new ConverseCommand(input), () => { throw error; })).toThrow(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.OK);
  });

  it.each(["invoke", "converse"])("%s streaming is lazy and closes the source on break", async (kind) => {
    const events = kind === "converse"
      ? [{ contentBlockDelta: { contentBlockIndex: 0, delta: { text: "Hi" } } }]
      : [{ chunk: { bytes: new TextEncoder().encode('{"outputText":"Hi"}') } }];
    let pulls = 0;
    let closed = false;
    const stream = (async function* () {
      try { for (const event of events) { pulls++; yield event; } pulls++; }
      finally { closed = true; }
    })();
    const key = kind === "converse" ? "stream" : "body";
    original.mockResolvedValue({ [key]: stream, $metadata: { requestId: "test" } });
    const command = kind === "converse" ? new ConverseStreamCommand(input) : new InvokeModelWithResponseStreamCommand(invokeInput);
    const response: any = await client.send(command as any);
    await new Promise(resolve => setImmediate(resolve));
    expect(pulls).toBe(0);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    for await (const event of response[key]) { expect(event).toBe(events[0]); break; }
    expect(pulls).toBe(1);
    expect(closed).toBe(true);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });
});
