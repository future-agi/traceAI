import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { TraceConfigOptions } from "@traceai/fi-core";
import { CerebrasInstrumentation } from "../instrumentation";

// The CI matrix supplies both the minimum and current real SDK, not a resource mock.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const sdk = require(
  process.env.CEREBRAS_SDK_PATH || "@cerebras/cerebras_cloud_sdk",
);
const positional = {
  model: "UNSENT_MODEL",
  messages: [{ role: "user", content: "UNSENT_INPUT" }],
  temperature: 0.9,
};
const effective = {
  model: "effective-model",
  messages: [{ role: "user", content: "EFFECTIVE_INPUT" }],
  temperature: 0.2,
};
const message = { role: "assistant", content: "OUTPUT_SENTINEL" };
const totals = { prompt_tokens: 12, completion_tokens: 6, total_tokens: 18 };

async function consume(result: unknown, streaming: boolean) {
  if (!streaming) return result;
  const chunks = [];
  for await (const chunk of result as AsyncIterable<unknown>) chunks.push(chunk);
  return chunks;
}

describe("Cerebras effective request and usage HTTP contracts", () => {
  let server: Server;
  let baseURL: string;
  let wire: string[];
  let optionHeaders: unknown[];
  let reply: unknown;
  let streaming: boolean;
  let exporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;
  let instrumentation: CerebrasInstrumentation | undefined;

  beforeEach(async () => {
    wire = [];
    optionHeaders = [];
    streaming = false;
    reply = { model: "response-model", choices: [], usage: totals };
    exporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    server = createServer((req, res) => {
      let body = "";
      req.on("data", (chunk) => (body += chunk.toString()));
      req.on("end", () => {
        wire.push(body);
        optionHeaders.push(req.headers["x-offline-option"]);
        if (streaming) {
          res.writeHead(200, { "content-type": "text/event-stream" });
          for (const chunk of reply as unknown[])
            res.write(`data: ${JSON.stringify(chunk)}\n\n`);
          res.end("data: [DONE]\n\n");
        } else {
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify(reply));
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterEach(async () => {
    instrumentation?.disable();
    instrumentation = undefined;
    await provider.shutdown();
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  });

  function client(traceConfig?: TraceConfigOptions) {
    instrumentation = new CerebrasInstrumentation({ traceConfig });
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
    return new sdk.Cerebras({
      apiKey: "synthetic-offline-key",
      baseURL,
      warmTCPConnection: false,
      maxRetries: 0,
    });
  }

  describe.each([false, true])("stream=%s", (stream) => {
    it.each([false, true])(
      "captures the replacement body, never positional data or options, hideInputs=%s",
      async (hideInputs) => {
        streaming = stream;
        if (stream) reply = [{ choices: [], usage: totals }];
        const c = client({ hideInputs });
        // Newer SDKs also expose native text completions.
        const operations = [
          { resource: c.chat.completions, input: positional, body: effective },
          ...(c.completions
            ? [{
                resource: c.completions,
                input: { model: "UNSENT_MODEL", prompt: "UNSENT_INPUT", temperature: 0.9 },
                body: { model: "effective-model", prompt: "EFFECTIVE_INPUT", temperature: 0.2 },
              }]
            : []),
        ];
        for (const { resource, input, body } of operations) {
          const original = Object.freeze({ ...input, stream });
          const replacement = Object.freeze({ ...body, stream });
          const options = Object.freeze({
            body: replacement,
            headers: { "x-offline-option": "OPTION_SENTINEL" },
          });
          expect(await consume(await resource.create(original, options), stream)).toEqual(reply);
          const sent = JSON.parse(wire[wire.length - 1]);
          expect(sent).toEqual(replacement);
          expect(optionHeaders[optionHeaders.length - 1]).toBe("OPTION_SENTINEL");
          const span = exporter.getFinishedSpans()[wire.length - 1];
          expect(span.attributes[S.LLM_MODEL_NAME]).toBe(sent.model);
          const serialized = JSON.stringify(span.attributes);
          expect(serialized).not.toMatch(/UNSENT_|OPTION_SENTINEL|synthetic-offline-key/);
          if (hideInputs) {
            expect(serialized).not.toContain("EFFECTIVE_INPUT");
          } else {
            expect(JSON.parse(span.attributes[S.INPUT_VALUE] as string)).toEqual(sent);
            expect(JSON.parse(span.attributes[S.RAW_INPUT] as string)).toEqual(sent);
            const { messages, prompt: _prompt, ...parameters } = sent;
            expect(JSON.parse(span.attributes[S.LLM_INVOCATION_PARAMETERS] as string)).toEqual(parameters);
            if (messages)
              expect(JSON.parse(span.attributes[S.LLM_INPUT_MESSAGES] as string)).toEqual(messages);
            else expect(span.attributes[S.LLM_INPUT_MESSAGES]).toBeUndefined();
          }
        }
        expect(exporter.getFinishedSpans()).toHaveLength(wire.length);
      },
    );

    it.each(["generated", "one-shot"])(
      "preserves an enumerable %s body accessor without tracing discarded input",
      async (kind) => {
        streaming = stream;
        if (stream) reply = [{ choices: [], usage: totals }];
        const c = client();
        const operations = [
          { resource: c.chat.completions, input: positional, body: effective },
          ...(c.completions
            ? [{
                resource: c.completions,
                input: { model: "UNSENT_MODEL", prompt: "UNSENT_INPUT" },
                body: { model: "effective-model", prompt: "EFFECTIVE_INPUT" },
              }]
            : []),
        ];
        for (const { resource, input, body } of operations) {
          async function invoke() {
            let reads = 0;
            const getter = jest.fn(function () {
              if (++reads > 1 && kind === "one-shot")
                throw new Error("body accessor was consumed twice");
              return { ...body, model: `model-${reads}`, stream };
            });
            const options = Object.freeze(Object.defineProperty({
              headers: { "x-offline-option": "OPTION_SENTINEL" },
            }, "body", { enumerable: true, get: getter }));
            const start = wire.length;
            let value: unknown;
            let error: unknown;
            try {
              value = await consume(
                await resource.create({ ...input, stream }, options),
                stream,
              );
            } catch (failure) {
              error = failure;
            }
            expect(getter.mock.contexts.every((receiver) => receiver === options)).toBe(true);
            return {
              reads, value, error,
              bodies: wire.slice(start),
              headers: optionHeaders.slice(start),
            };
          }
          instrumentation!.disable();
          const expected = await invoke();
          expect(expected).toMatchObject({
            reads: 1,
            value: reply,
            error: undefined,
            headers: ["OPTION_SENTINEL"],
          });
          expect(expected.bodies.map((sent) => JSON.parse(sent))).toEqual([
            { ...body, model: "model-1", stream },
          ]);
          instrumentation!.enable();
          expect(await invoke()).toEqual(expected);
          // An accessor is opaque: do not export its value or the unsent input.
          expect(exporter.getFinishedSpans()).toHaveLength(0);
        }
      },
    );

    describe.each([
      undefined,
      "hideOutputs",
      "hideOutputMessages",
      "hideOutputText",
    ] as const)("privacy=%s", (flag) => {
      it.each([
        { cached: 8, reasoning: 4 },
        { cached: 0, reasoning: 0 },
        { cached: undefined, reasoning: 4 },
        { cached: 8, reasoning: undefined },
        { cached: null, reasoning: null },
        { cached: "8", reasoning: "4" },
      ])("retains numeric usage details independently: %j", async ({ cached, reasoning }) => {
        const usage = {
          ...totals,
          prompt_tokens_details: { cached_tokens: cached },
          completion_tokens_details: { reasoning_tokens: reasoning },
        };
        streaming = stream;
        reply = stream
          ? [
              { choices: [{ index: 0, delta: message, finish_reason: "stop" }] },
              // Usage-only chunk followed by a chunk without usage must not erase it.
              { choices: [], usage },
              { choices: [] },
            ]
          : { choices: [{ index: 0, message, finish_reason: "stop" }], usage };
        const c = client(flag ? { [flag]: true } : {});
        expect(await consume(await c.chat.completions.create({ ...effective, stream }), stream)).toEqual(reply);
        expect(exporter.getFinishedSpans()).toHaveLength(1);
        const attrs = exporter.getFinishedSpans()[0].attributes;
        expect(attrs[S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]).toBe(
          typeof cached === "number" ? cached : undefined,
        );
        expect(attrs[S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]).toBe(
          typeof reasoning === "number" ? reasoning : undefined,
        );
        expect(attrs).toMatchObject({
          [S.LLM_TOKEN_COUNT_PROMPT]: 12,
          [S.LLM_TOKEN_COUNT_COMPLETION]: 6,
          [S.LLM_TOKEN_COUNT_TOTAL]: 18,
        });
        expect(JSON.stringify(attrs).includes("OUTPUT_SENTINEL")).toBe(!flag);
        if (flag) {
          expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
          expect(attrs[S.LLM_OUTPUT_MESSAGES]).toBeUndefined();
        }
      });
    });
  });

  it.each([undefined, null, "replacement", false, 0, []])(
    "forwards a non-request override %j without tracing replaced input",
    async (body) => {
      const c = client();
      const options = { body };
      // Compare with the same SDK uninstrumented, including SDK coercion to no body.
      instrumentation!.disable();
      const expected = await c.chat.completions.create(positional, options);
      instrumentation!.enable();
      expect(await c.chat.completions.create(positional, options)).toEqual(expected);
      expect(wire).toHaveLength(2);
      expect(wire[1]).toBe(wire[0]);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
    },
  );

  it("preserves a body accessor's original error without issuing HTTP or tracing input", async () => {
    const c = client();
    const error = new Error("body accessor failure");
    const getter = jest.fn(() => { throw error; });
    const options = Object.defineProperty({}, "body", {
      enumerable: true,
      get: getter,
    });
    for (const enabled of [false, true]) {
      if (enabled) instrumentation!.enable();
      else instrumentation!.disable();
      await expect((async () => c.chat.completions.create(positional, options))())
        .rejects.toBe(error);
      expect(getter).toHaveBeenCalledTimes(enabled ? 2 : 1);
      expect(wire).toHaveLength(0);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
    }
  });

  it("forwards a setter-only body override without tracing unsent input", async () => {
    const c = client();
    const setter = jest.fn();
    const options = Object.defineProperty({}, "body", {
      enumerable: true,
      set: setter,
    });
    instrumentation!.disable();
    const expected = await c.chat.completions.create(positional, options);
    instrumentation!.enable();
    expect(await c.chat.completions.create(positional, options)).toEqual(expected);
    expect(wire).toHaveLength(2);
    expect(wire[1]).toBe(wire[0]);
    expect(setter).not.toHaveBeenCalled();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it.each(["inherited accessor", "non-enumerable accessor"])(
    "does not read an ignored %s body property",
    async (kind) => {
      const getter = jest.fn(() => { throw new Error("ignored body getter"); });
      const holder = Object.defineProperty({}, "body", {
        enumerable: kind === "inherited accessor",
        get: getter,
      });
      const options = kind === "inherited accessor" ? Object.create(holder) : holder;
      const c = client();
      await c.chat.completions.create(positional, options);
      expect(getter).not.toHaveBeenCalled();
      expect(JSON.parse(wire[0])).toEqual(positional);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(JSON.parse(exporter.getFinishedSpans()[0].attributes[S.INPUT_VALUE] as string))
        .toEqual(positional);
    },
  );

  it.each(["inherited", "non-enumerable"])(
    "does not treat an %s options.body as an SDK override",
    async (kind) => {
      const options = kind === "inherited"
        ? Object.create({ body: effective })
        : Object.defineProperty({}, "body", { value: effective });
      const c = client();
      await c.chat.completions.create(positional, options);
      const sent = JSON.parse(wire[0]);
      expect(sent).toEqual(positional);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      const attrs = exporter.getFinishedSpans()[0].attributes;
      expect(JSON.parse(attrs[S.INPUT_VALUE] as string)).toEqual(sent);
      expect(JSON.parse(attrs[S.RAW_INPUT] as string)).toEqual(sent);
      expect(JSON.parse(attrs[S.LLM_INPUT_MESSAGES] as string)).toEqual(sent.messages);
      expect(attrs[S.LLM_MODEL_NAME]).toBe(sent.model);
    },
  );
});
