import { describe, it, expect, jest } from "@jest/globals";
import {
  ConverseCommand,
  InvokeModelCommand,
  InvokeModelWithResponseStreamCommand,
} from "@aws-sdk/client-bedrock-runtime";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { SpanStatusCode } from "@opentelemetry/api";
import { BedrockInstrumentation } from "../instrumentation";

const modelId = "amazon.nova-pro-v1:0";
const messages = [{ role: "user" as const, content: [{ text: "hello" }] }];

class UnrecognizedCommand {}

const commands: [string, () => unknown][] = [
  ["InvokeModel", () => new InvokeModelCommand({ modelId, body: JSON.stringify({ messages }) })],
  ["InvokeModelWithResponseStream", () => new InvokeModelWithResponseStreamCommand({ modelId, body: JSON.stringify({ messages }) })],
  ["Converse", () => new ConverseCommand({ modelId, messages })],
  ["unrecognized", () => new UnrecognizedCommand()],
];

type Callback = (this: unknown, error: unknown, response: unknown) => void;

/** SDK-shaped double: callback sends return void; promise sends return a Promise. */
function recordingClient({
  response = {},
  error,
  synchronousCallback = false,
  synchronousError,
}: {
  response?: unknown;
  error?: Error;
  synchronousCallback?: boolean;
  synchronousError?: Error;
} = {}) {
  const calls: { receiver: unknown; args: unknown[] }[] = [];
  const callbackReceiver = {};
  let pendingCallback: Callback | undefined;
  const complete = () => pendingCallback?.call(callbackReceiver, error, response);

  class BedrockRuntimeClient {
    send(...args: unknown[]): Promise<unknown> | void {
      calls.push({ receiver: this, args });
      if (synchronousError) throw synchronousError;
      const callback = typeof args[1] === "function" ? args[1] : args[2];
      if (typeof callback === "function") {
        pendingCallback = callback as Callback;
        if (synchronousCallback) complete();
        return;
      }
      return error ? Promise.reject(error) : Promise.resolve(response);
    }
  }

  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const instrumentation = new BedrockInstrumentation({
    instrumentationConfig: { enabled: false },
    tracerProvider: provider,
  });
  instrumentation.manuallyInstrument({
    BedrockRuntimeClient,
  } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);

  return { client: new BedrockRuntimeClient(), calls, complete, callbackReceiver, exporter };
}

function abortedOptions() {
  const controller = new AbortController();
  controller.abort();
  return { abortSignal: controller.signal, requestTimeout: 1 };
}

describe("T8-TS-CLOUD-001: Bedrock send overloads", () => {
  describe.each(commands)("%s", (name, makeCommand) => {
    it("forwards promise options and the original receiver", async () => {
      const response = {};
      const { client, calls } = recordingClient({ response });
      const command = makeCommand();
      const options = abortedOptions();

      const result = client.send(command, options);
      expect(result).toBeInstanceOf(Promise);
      await expect(result).resolves.toBe(response);
      expect(calls).toHaveLength(1);
      expect(calls[0].receiver).toBe(client);
      expect(calls[0].args).toHaveLength(2);
      expect(calls[0].args[0]).toBe(command);
      expect(calls[0].args[1]).toBe(options);
    });

    it("preserves promise rejections", async () => {
      const error = new Error("offline failure");
      const { client, calls } = recordingClient({ error });
      const options = abortedOptions();
      await expect(client.send(makeCommand(), options)).rejects.toBe(error);
      expect(calls[0].args[1]).toBe(options);
    });

    it.each(["callback", "options + callback", "undefined + callback"])(
      "preserves the %s overload, return value and callback receiver",
      async (overload) => {
        const response = {};
        const { client, calls, complete, callbackReceiver, exporter } = recordingClient({ response });
        const command = makeCommand();
        const options = abortedOptions();
        let receiver: unknown;
        const callback = jest.fn(function (this: unknown, _error: unknown, _response: unknown) {
          receiver = this;
        });
        const args = overload === "callback"
          ? [command, callback]
          : [command, overload === "options + callback" ? options : undefined, callback];

        const result = client.send(...args);
        // Drain the incorrect promise on the red revision without leaving work behind.
        if (result instanceof Promise) await result;
        expect(callback).not.toHaveBeenCalled();
        complete();

        expect(result).toBeUndefined();
        expect(callback).toHaveBeenCalledTimes(1);
        expect(callback).toHaveBeenCalledWith(undefined, response);
        expect(receiver).toBe(callbackReceiver);
        expect(calls).toHaveLength(1);
        expect(calls[0].receiver).toBe(client);
        expect(calls[0].args).toHaveLength(args.length);
        expect(calls[0].args[0]).toBe(command);
        if (args.length === 3) expect(calls[0].args[1]).toBe(args[1]);
        expect(exporter.getFinishedSpans()).toHaveLength(name === "unrecognized" ? 0 : 1);
      },
    );

    it.each([false, true])("preserves callback errors (options: %s)", async (withOptions) => {
      const error = new Error("offline callback failure");
      const response = {};
      const { client, complete, exporter } = recordingClient({ error, response });
      const callback = jest.fn<Callback>();
      const command = makeCommand();
      const result = withOptions
        ? client.send(command, abortedOptions(), callback)
        : client.send(command, callback);
      if (result instanceof Promise) await result.catch(() => undefined);
      complete();

      expect(result).toBeUndefined();
      expect(callback).toHaveBeenCalledTimes(1);
      expect(callback).toHaveBeenCalledWith(error, response);
      if (name !== "unrecognized") {
        const spans = exporter.getFinishedSpans();
        expect(spans).toHaveLength(1);
        expect(spans[0].status).toEqual({ code: SpanStatusCode.ERROR, message: error.message });
      }
    });

    it("preserves synchronous send errors without invoking the callback", () => {
      const error = new Error("invalid offline options");
      const { client, calls } = recordingClient({ synchronousError: error });
      const callback = jest.fn<Callback>();
      const options = abortedOptions();
      expect(() => client.send(makeCommand(), options, callback)).toThrow(error);
      expect(callback).not.toHaveBeenCalled();
      expect(calls[0].args[1]).toBe(options);
      expect(calls[0].args).toHaveLength(3);
    });

    it("does not swallow, retry or record an application callback exception", () => {
      const error = new Error("application callback failure");
      const { client, exporter } = recordingClient({ synchronousCallback: true });
      const callback = jest.fn(() => { throw error; });

      expect(() => client.send(makeCommand(), callback)).toThrow(error);
      expect(callback).toHaveBeenCalledTimes(1);
      const spans = exporter.getFinishedSpans();
      expect(spans.some((span) => span.status.message === error.message)).toBe(false);
    });
  });

  it("passes every trailing argument and the exact return value through for unknown commands", () => {
    const result = {};
    const calls: unknown[][] = [];
    class BedrockRuntimeClient {
      send(...args: unknown[]) {
        calls.push(args);
        return result;
      }
    }
    const instrumentation = new BedrockInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentation.manuallyInstrument({
      BedrockRuntimeClient,
    } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
    const client = new BedrockRuntimeClient();
    const args = [new UnrecognizedCommand(), abortedOptions(), undefined, "extra argument"];
    expect(client.send(...args)).toBe(result);
    expect(calls).toEqual([args]);
  });

  it.each(["promise", "callback"])("preserves stream chunks with %s send", async (overload) => {
    const chunks = [
      { chunk: { bytes: new TextEncoder().encode('{"contentBlockDelta":{"delta":{"text":"hello"}}}') } },
      { chunk: { bytes: new TextEncoder().encode('{"metadata":{"usage":{"inputTokens":1,"outputTokens":1}}}') } },
    ];
    const response = {
      body: (async function* () { yield* chunks; })(),
      contentType: "application/json",
    };
    const { client, calls, complete } = recordingClient({ response });
    const command = new InvokeModelWithResponseStreamCommand({ modelId, body: JSON.stringify({ messages }) });
    const options = abortedOptions();
    let output: unknown;
    if (overload === "callback") {
      const callback = jest.fn((_error: unknown, value: unknown) => { output = value; });
      const result = client.send(command, options, callback);
      if (result instanceof Promise) await result;
      complete();
      expect(result).toBeUndefined();
      expect(callback).toHaveBeenCalledTimes(1);
    } else {
      output = await client.send(command, options);
    }

    const streamResponse = output as { body: AsyncIterable<unknown>; contentType: string };
    const received: unknown[] = [];
    for await (const chunk of streamResponse.body) received.push(chunk);
    expect(received).toEqual(chunks);
    expect(streamResponse.contentType).toBe(response.contentType);
    expect(calls[0].args[1]).toBe(options);
  });
});
