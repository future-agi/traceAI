import { BedrockRuntimeClient, ConverseCommand, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { BedrockInstrumentation } from "../instrumentation";

/** Exercise Smithy's actual send overload dispatch with an in-memory HTTP handler. */
describe("real SDK send contracts", () => {
  const originalSend = BedrockRuntimeClient.prototype.send;
  let instrumentation: BedrockInstrumentation;
  let provider: BasicTracerProvider;
  let exporter: InMemorySpanExporter;
  let client: BedrockRuntimeClient;
  let handle: jest.Mock;

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    instrumentation = new BedrockInstrumentation({ instrumentationConfig: { enabled: false }, tracerProvider: provider });
    instrumentation.manuallyInstrument({ BedrockRuntimeClient });
    handle = jest.fn(async () => ({ response: {
      statusCode: 200,
      headers: { "content-type": "application/json" },
      body: new TextEncoder().encode(JSON.stringify({ output: { message: { role: "assistant", content: [{ text: "Hello" }] } }, usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 } })),
    } }));
    client = new BedrockRuntimeClient({
      region: "us-east-1", maxAttempts: 1,
      credentials: { accessKeyId: "synthetic", secretAccessKey: "synthetic" },
      requestHandler: { handle },
    });
  });
  afterEach(async () => {
    instrumentation.disable();
    BedrockRuntimeClient.prototype.send = originalSend;
    client.destroy();
    await provider.shutdown();
  });

  const commands = [
    () => new ConverseCommand({ modelId: "amazon.nova-lite-v1:0", messages: [{ role: "user", content: [{ text: "Hi" }] }] }),
    () => new InvokeModelCommand({ modelId: "amazon.nova-lite-v1:0", body: JSON.stringify({ messages: [{ role: "user", content: [{ text: "Hi" }] }] }) }),
  ];
  it.each(commands)("forwards Promise request options through Smithy", async command => {
    const options = { abortSignal: new AbortController().signal, requestTimeout: 100 };
    const response = await client.send(command() as any, options);
    expect(response.$metadata.httpStatusCode).toBe(200);
    expect(handle.mock.calls[0][1]).toBe(options);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it.each(commands)("preserves both real callback overloads", async command => {
    for (const withOptions of [false, true]) {
      exporter.reset();
      handle.mockClear();
      const options = { abortSignal: new AbortController().signal, requestTimeout: 100 };
      await new Promise<void>((resolve, reject) => {
        const callback = (error: unknown, response: any) => {
          if (error) { reject(error); return; }
          try {
            expect(response.$metadata.httpStatusCode).toBe(200);
            if (withOptions) expect(handle.mock.calls[0][1]).toBe(options);
            expect(exporter.getFinishedSpans()).toHaveLength(1);
            resolve();
          } catch (failure) { reject(failure); }
        };
        const result = withOptions
          ? client.send(command() as any, options, callback)
          : client.send(command() as any, callback);
        expect(result).toBeUndefined();
      });
    }
  });
});
