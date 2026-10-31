import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { CerebrasInstrumentation } from "../instrumentation";

// eslint-disable-next-line @typescript-eslint/no-var-requires
const sdk = require(
  process.env.CEREBRAS_SDK_PATH || "@cerebras/cerebras_cloud_sdk",
);
interface WireSpan {
  name: string;
  status: { code?: number };
  attributes: Array<{
    key: string;
    value: { stringValue?: string; intValue?: number };
  }>;
}
interface Payload {
  resourceSpans: Array<{ scopeSpans: Array<{ spans: WireSpan[] }> }>;
}

describe("Cerebras OTLP HTTP wire contract", () => {
  let server: Server;
  let provider: NodeTracerProvider;
  let instrumentation: CerebrasInstrumentation;
  let payloads: Payload[];
  beforeEach(async () => {
    payloads = [];
    server = createServer((req, res) => {
      let body = "";
      req.on("data", (chunk) => {
        body += chunk.toString();
      });
      req.on("end", () => {
        expect(req.url).toBe("/v1/traces");
        payloads.push(JSON.parse(body));
        res.writeHead(200, { "content-type": "application/json" }).end("{}");
      });
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", resolve),
    );
    const port = (server.address() as AddressInfo).port;
    provider = new NodeTracerProvider({
      spanProcessors: [
        new SimpleSpanProcessor(
          new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` }),
        ),
      ],
    });
  });
  afterEach(async () => {
    instrumentation?.disable();
    await provider.shutdown();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  });

  it.each([false, true])(
    "exports attributed spans with hideInputs/hideOutputs=%s",
    async (hide) => {
      instrumentation = new CerebrasInstrumentation({
        traceConfig: { hideInputs: hide, hideOutputs: hide },
      });
      instrumentation.setTracerProvider(provider);
      instrumentation.manuallyInstrument(sdk);
      const reply = {
        id: "wire-1",
        model: "wire-model",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "OUTPUT_SENTINEL" },
            finish_reason: "stop",
          },
        ],
        usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 },
        time_info: { total_time: 0.12 },
      };
      const client = new sdk.Cerebras({
        apiKey: "offline-test-key",
        baseURL: "http://127.0.0.1:9",
        warmTCPConnection: false,
        maxRetries: 0,
        fetch: async () =>
          new Response(JSON.stringify(reply), {
            headers: { "content-type": "application/json" },
          }),
      });
      const request = {
        model: "wire-model",
        messages: [{ role: "user", content: "INPUT_SENTINEL" }],
        stop: ["PARAM_SENTINEL"],
      };
      await client.chat.completions.create(request);
      if (client.completions)
        await client.completions.create({
          model: "wire-model",
          prompt: "INPUT_SENTINEL",
          stop: ["PARAM_SENTINEL"],
        });
      await provider.forceFlush();
      const spans = payloads.flatMap((p) =>
        p.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
      );
      expect(spans).toHaveLength(client.completions ? 2 : 1);
      for (const span of spans) {
        const attrs = Object.fromEntries(
          span.attributes.map((a) => [a.key, a.value]),
        );
        expect(attrs[S.LLM_PROVIDER]).toEqual({ stringValue: "cerebras" });
        expect(attrs[S.GEN_AI_RESPONSE_MODEL]).toEqual({
          stringValue: "wire-model",
        });
        expect(Number(attrs[S.LLM_TOKEN_COUNT_TOTAL].intValue)).toBe(6);
        expect(span.status.code).toBe(1);
      }
      const wire = JSON.stringify(payloads);
      for (const sentinel of [
        "INPUT_SENTINEL",
        "OUTPUT_SENTINEL",
        "PARAM_SENTINEL",
      ]) {
        expect(wire.includes(sentinel)).toBe(!hide);
      }
    },
  );

  it("redacts stream payloads and SDK error details on the wire", async () => {
    instrumentation = new CerebrasInstrumentation({
      traceConfig: { hideInputs: true, hideOutputs: true },
    });
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
    const chunks = [
      {
        id: "stream-1",
        model: "wire-model",
        choices: [{ index: 0, delta: { content: "OUTPUT_SENTINEL" } }],
      },
      { error: { message: "ERROR_SENTINEL" } },
    ];
    const client = new sdk.Cerebras({
      apiKey: "offline-test-key",
      baseURL: "http://127.0.0.1:9",
      warmTCPConnection: false,
      maxRetries: 0,
      fetch: async () =>
        new Response(
          chunks.map((c) => `data: ${JSON.stringify(c)}\n\n`).join(""),
          { headers: { "content-type": "text/event-stream" } },
        ),
    });
    const stream = await client.chat.completions.create({
      model: "wire-model",
      messages: [{ role: "user", content: "INPUT_SENTINEL" }],
      stream: true,
    });
    await expect(
      (async () => {
        for await (const _ of stream) {
          /* consume */
        }
      })(),
    ).rejects.toThrow();
    await provider.forceFlush();
    const spans = payloads.flatMap((p) =>
      p.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
    );
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(2);
    expect(JSON.stringify(payloads)).not.toMatch(
      /INPUT_SENTINEL|OUTPUT_SENTINEL|ERROR_SENTINEL/,
    );
  });
});
