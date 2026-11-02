const http = require("node:http");
const { once } = require("node:events");
const { setTimeout: delay } = require("node:timers/promises");
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");
const {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} = require("@opentelemetry/sdk-trace-base");
const {
  OTLPTraceExporter,
} = require("@opentelemetry/exporter-trace-otlp-http");
const { ElevenLabsInstrumentation } = require(
  process.env.TRACEAI_PACKAGE || "../dist/src",
);

const INPUT = "synthetic-input-elevenlabs";
const OUTPUT = "synthetic-transcript-elevenlabs";
const KEY = "synthetic-elevenlabs-key-NOT-REAL";
const AUDIO = Buffer.from([0, 255, 128, 1, 16, 20, 30, 40, 41]);
const BASE64 = "c3ludGhldGljLWF1ZGlvLW11c3Qtbm90LWJlLWV4cG9ydGVk";
const timestamp = {
  audio_base64: BASE64,
  alignment: {
    characters: ["a"],
    character_start_times_seconds: [0],
    character_end_times_seconds: [0.1],
  },
  normalized_alignment: null,
};
const transcript = {
  text: OUTPUT,
  language_code: "eng",
  language_probability: 0.99,
  words: [],
};
const attributes = (span) =>
  Object.fromEntries(
    (span.attributes || []).map((a) => [
      a.key,
      a.value.stringValue ??
        a.value.doubleValue ??
        a.value.intValue ??
        a.value.boolValue,
    ]),
  );

async function fixture(SDK, options = {}) {
  const requests = [],
    wire = [];
  const attempts = new Map();
  const sockets = new Set();
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = Buffer.concat(chunks);
      if (req.url === "/v1/traces") {
        wire.push({
          path: req.url,
          headers: req.headers,
          payload: JSON.parse(body.toString()),
        });
        res.setHeader("content-type", "application/json");
        res.end("{}");
        return;
      }
      requests.push({ url: req.url, headers: req.headers, body });
      const mode = req.headers["x-case"] || "normal";
      const count = (attempts.get(mode) || 0) + 1;
      attempts.set(mode, count);
      if (/^error-/.test(mode) || (mode === "retry" && count === 1)) {
        res.statusCode = mode === "retry" ? 429 : Number(mode.slice(6));
        res.setHeader("content-type", "application/json");
        res.setHeader("retry-after", "0");
        res.end(
          JSON.stringify({
            detail: [
              {
                type: "synthetic-error-body",
                msg: `${KEY} ${INPUT} ${OUTPUT} /private/secret.wav https://invalid.test/?token=SIGNED_URL`,
              },
            ],
          }),
        );
        return;
      }
      if (mode === "timeout") {
        await delay(250);
        if (res.destroyed) return;
      }
      res.setHeader("x-request-id", "synthetic-request-id");
      if (req.url.startsWith("/v1/speech-to-text")) {
        res.setHeader("content-type", "application/json");
        if (body.includes(Buffer.from('name="webhook"\r\n\r\ntrue')))
          res.end(
            JSON.stringify({
              message: "submitted",
              request_id: "synthetic-job",
            }),
          );
        else
          res.end(
            JSON.stringify(
              mode === "multi"
                ? {
                    transcripts: [
                      transcript,
                      {
                        ...transcript,
                        channel_index: 1,
                        text: OUTPUT + "-channel2",
                      },
                    ],
                  }
                : mode === "long"
                  ? { ...transcript, text: "X".repeat(10000) }
                  : transcript,
            ),
          );
        return;
      }
      if (req.url.includes("with-timestamps")) {
        res.setHeader("content-type", "application/json");
        if (req.url.includes("/stream/")) {
          res.write(JSON.stringify(timestamp) + "\n");
          await delay(15);
          res.end(
            mode === "bad-stream"
              ? "{bad-json}\n"
              : JSON.stringify(timestamp) + "\n",
          );
        } else res.end(JSON.stringify(timestamp));
        return;
      }
      res.setHeader("content-type", "audio/mpeg");
      res.flushHeaders();
      await delay(mode === "slow" ? 120 : 10);
      if (res.destroyed) return;
      res.write(AUDIO.subarray(0, 4));
      await delay(mode === "slow" ? 120 : 10);
      if (res.destroyed) return;
      if (mode === "broken") res.destroy();
      else res.end(AUDIO.subarray(4));
    } catch {
      res.destroy();
    }
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const memory = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({
    spanProcessors: [
      new SimpleSpanProcessor(memory),
      new SimpleSpanProcessor(
        new OTLPTraceExporter({
          url: baseUrl + "/v1/traces",
          headers: {
            FI_API_KEY: "synthetic-fi-api",
            FI_SECRET_KEY: "synthetic-fi-secret",
          },
        }),
      ),
    ],
  });
  const instrumentation = new ElevenLabsInstrumentation({
    tracerProvider: provider,
    ...options,
  });
  const plain = new SDK.ElevenLabsClient({
    apiKey: KEY,
    baseUrl,
    maxRetries: 0,
  });
  const client = instrumentation.wrapClient(plain);
  return {
    requests,
    wire,
    attempts,
    provider,
    memory,
    instrumentation,
    plain,
    client,
    baseUrl,
    spans: () =>
      memory
        .getFinishedSpans()
        .filter((s) => s.instrumentationScope.name === "@traceai/elevenlabs"),
    async flush() {
      await provider.forceFlush();
      return wire.flatMap((w) =>
        w.payload.resourceSpans.flatMap((r) =>
          r.scopeSpans.flatMap((s) => s.spans),
        ),
      );
    },
    async close() {
      instrumentation.shutdown();
      await provider.shutdown();
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}
async function bytes(stream, mode = "reader") {
  const chunks = [];
  if (mode === "pipe")
    await stream.pipeTo(
      new WritableStream({
        write(chunk) {
          chunks.push(chunk);
        },
      }),
    );
  else if (mode === "iterator") {
    for await (const chunk of stream) chunks.push(chunk);
  } else {
    const reader = stream.getReader();
    for (;;) {
      const next = await reader.read();
      if (next.done) break;
      chunks.push(next.value);
    }
    reader.releaseLock();
  }
  return Buffer.concat(chunks);
}
module.exports = {
  fixture,
  bytes,
  attributes,
  INPUT,
  OUTPUT,
  KEY,
  AUDIO,
  BASE64,
  timestamp,
  delay,
};
