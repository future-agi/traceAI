const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { Readable } = require("node:stream");
const { mkdtemp, writeFile, rm } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { context, trace } = require("@opentelemetry/api");
const { suppressTracing } = require("@opentelemetry/core");
const {
  AsyncLocalStorageContextManager,
} = require("@opentelemetry/context-async-hooks");
const { setSession, setUser } = require("@traceai/fi-core");
const {
  fixture,
  bytes,
  attributes,
  INPUT,
  OUTPUT,
  KEY,
  AUDIO,
  BASE64,
  delay,
} = require("./fixture.cjs");
const manager = new AsyncLocalStorageContextManager();
before(() => context.setGlobalContextManager(manager.enable()));
after(() => {
  context.disable();
  manager.disable();
});
const request = {
  text: INPUT,
  modelId: "eleven_multilingual_v2",
  outputFormat: "mp3_44100_128",
};
const opts = (mode) => ({ headers: { "x-case": mode }, maxRetries: 0 });

for (const name of ["@elevenlabs/sdk-floor", "@elevenlabs/elevenlabs-js"]) {
  const SDK = require(name);
  async function run(t, config = {}) {
    const f = await fixture(SDK, config);
    t.after(() => f.close());
    return f;
  }

  test(`${name}: five real methods, promise contract, decoded OTLP and lossless audio`, async (t) => {
    const f = await run(t);
    for (const [method, consumption] of [
      ["convert", "reader"],
      ["stream", "pipe"],
    ]) {
      const response = f.client.textToSpeech[method](
        "voice-id",
        request,
        opts("normal"),
      );
      assert.equal(response.constructor.name, "HttpResponsePromise");
      assert.ok(response instanceof Promise);
      let finalized = false;
      const [raw, stream, viaThen, viaFinally] = await Promise.all([
        response.withRawResponse(),
        response,
        response.then((x) => x),
        response.finally(() => {
          finalized = true;
        }),
      ]);
      assert.ok(finalized);
      assert.equal(raw.data, stream);
      assert.equal(stream, viaThen);
      assert.equal(stream, viaFinally);
      assert.equal(raw.rawResponse.status, 200);
      assert.equal(
        raw.rawResponse.headers.get("x-request-id"),
        "synthetic-request-id",
      );
      assert.equal(f.requests.length, method === "convert" ? 1 : 2);
      assert.equal(
        f.spans().length,
        method === "convert" ? 0 : 1,
        "no success at HTTP headers",
      );
      assert.deepEqual(await bytes(stream, consumption), AUDIO);
      assert.equal(
        await response,
        stream,
        "repeat await returns identical stream",
      );
    }
    const stamped = await f.client.textToSpeech.convertWithTimestamps(
      "voice-id",
      request,
    );
    assert.equal(stamped.audioBase64, BASE64);
    assert.deepEqual(stamped.alignment.characters, ["a"]);
    const timestampStream = await f.client.textToSpeech.streamWithTimestamps(
      "voice-id",
      request,
    );
    const stampedChunks = [];
    for await (const chunk of timestampStream) stampedChunks.push(chunk);
    assert.equal(stampedChunks.length, 2);
    assert.deepEqual(stampedChunks[0], stampedChunks[1]);
    assert.equal(stampedChunks[0].audioBase64, BASE64);
    const transcript = await f.client.speechToText.convert({
      modelId: "scribe_v2",
      languageCode: "eng",
      file: new Blob([AUDIO], { type: "audio/wav" }),
    });
    assert.equal(transcript.text, OUTPUT);
    assert.equal(f.requests.length, 5);
    const body = JSON.parse(f.requests[0].body);
    assert.equal(body.text, INPUT);
    assert.equal(body.model_id, request.modelId);
    assert.match(f.requests[0].url, /output_format=mp3_44100_128/);
    assert.ok(f.requests[4].body.includes(AUDIO));
    assert.equal(f.requests[0].headers["xi-api-key"], KEY);
    const wire = await f.flush();
    assert.equal(wire.length, 5);
    assert.equal(new Set(wire.map((s) => s.spanId)).size, 5);
    for (const span of wire) {
      const a = attributes(span);
      assert.match(span.traceId, /^[a-f0-9]{32}$/);
      assert.match(span.spanId, /^[a-f0-9]{16}$/);
      assert.ok(BigInt(span.endTimeUnixNano) >= BigInt(span.startTimeUnixNano));
      assert.equal(span.kind, 3);
      assert.equal(a["gen_ai.provider.name"], "elevenlabs");
      assert.equal(a["elevenlabs.completion"], "complete");
      assert.equal(span.status.code, 1);
      assert.ok(a["gen_ai.request.model"]);
      if (a["gen_ai.operation.name"] === "speech")
        assert.ok(a["elevenlabs.first_audio_latency_ms"] >= 0);
    }
    const serialized = JSON.stringify(f.wire.map((w) => w.payload));
    for (const forbidden of [
      KEY,
      BASE64,
      "audio_base64",
      "synthetic-fi-api",
      "synthetic-fi-secret",
    ])
      assert.ok(!serialized.includes(forbidden));
    assert.ok(f.wire.every((w) => w.path === "/v1/traces"));
    assert.ok(
      f.wire.every(
        (w) =>
          w.headers.fi_api_key === "synthetic-fi-api" &&
          w.headers.fi_secret_key === "synthetic-fi-secret",
      ),
    );
  });

  test(`${name}: application-driven reads, iteration and large streams without audio retention`, async (t) => {
    const f = await run(t);
    let pulls = 0,
      cancelled = 0;
    const chunk = new Uint8Array(64 * 1024).fill(37);
    const source = new ReadableStream(
      {
        pull(controller) {
          if (pulls === 256) controller.close();
          else {
            pulls++;
            controller.enqueue(chunk);
          }
        },
        cancel() {
          cancelled++;
        },
      },
      { highWaterMark: 0 },
    );
    const raw = new SDK.ElevenLabsClient({
      apiKey: KEY,
      fetch: async () =>
        new Response(source, { headers: { "content-type": "audio/mpeg" } }),
    });
    const stream = await f.instrumentation
      .wrapClient(raw)
      .textToSpeech.convert("voice-id", request);
    await delay(20);
    assert.equal(
      pulls,
      0,
      "instrumentation must not pull before the application",
    );
    assert.equal(f.spans().length, 0);
    let total = 0;
    for await (const value of stream) {
      total += value.byteLength;
      assert.equal(value[0], 37);
    }
    assert.equal(total, 16 * 1024 * 1024);
    assert.equal(pulls, 256);
    assert.equal(cancelled, 0);
    assert.equal(f.spans()[0].attributes["elevenlabs.audio.bytes"], total);
    const plain = await f.plain.textToSpeech.convert("voice-id", request);
    const traced = await f.client.textToSpeech.convert("voice-id", request);
    assert.deepEqual(
      await bytes(plain, "iterator"),
      await bytes(traced, "iterator"),
    );
  });

  test(`${name}: first audio includes delayed customer consumption`, async (t) => {
    const f = await run(t);
    const stream = await f.client.textToSpeech.stream("voice-id", request);
    await delay(65);
    assert.equal(f.spans().length, 0);
    await bytes(stream);
    assert.ok(
      f.spans()[0].attributes["elevenlabs.first_audio_latency_ms"] >= 60,
    );
  });

  test(`${name}: upload Blob, streamed bytes, path and multi-channel results`, async (t) => {
    const f = await run(t);
    const directory = await mkdtemp(join(tmpdir(), "elevenlabs-synthetic-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const path = join(directory, "private-synthetic-audio.wav");
    await writeFile(path, AUDIO);
    const uploads = [
      new Blob([AUDIO]),
      Readable.from([AUDIO.subarray(0, 3), AUDIO.subarray(3)]),
      { path },
      new File([AUDIO], "synthetic-file.wav"),
    ];
    for (const file of uploads) {
      const output = await f.client.speechToText.convert({
        file,
        modelId: "scribe_v2",
        languageCode: "eng",
      });
      assert.equal(output.text, OUTPUT);
      assert.ok(f.requests.at(-1).body.includes(AUDIO));
    }
    const multi = await f.client.speechToText.convert(
      { modelId: "scribe_v2", file: new Blob([AUDIO]), useMultiChannel: true },
      opts("multi"),
    );
    assert.equal(multi.transcripts.length, 2);
    assert.equal(
      f.spans().at(-1).attributes["output.value"],
      OUTPUT + "\n" + OUTPUT + "-channel2",
    );
    const wire = JSON.stringify(await f.flush());
    for (const forbidden of [
      path,
      "private-synthetic-audio",
      "synthetic-file.wav",
      BASE64,
      KEY,
    ])
      assert.ok(!wire.includes(forbidden));
  });

  test(`${name}: webhook acknowledgement and realtime are untouched`, async (t) => {
    const f = await run(t);
    assert.equal(f.client.speechToText.realtime, f.plain.speechToText.realtime);
    assert.equal(
      f.client.textToSpeech.streamInput,
      f.plain.textToSpeech.streamInput,
    );
    const response = await f.client.speechToText.convert({
      modelId: "scribe_v2",
      file: new Blob([AUDIO]),
      webhook: true,
    });
    assert.equal(response.message, "submitted");
    assert.equal(f.spans().length, 0);
  });

  for (const status of [422, 429, 500])
    test(`${name}: preserves ${status} exceptions through catch/finally/raw`, async (t) => {
      const f = await run(t);
      const response = f.client.textToSpeech.convert(
        "voice-id",
        request,
        opts("error-" + status),
      );
      let caught;
      await response.catch((error) => {
        caught = error;
      });
      assert.equal(caught.statusCode, status);
      await assert.rejects(
        response.withRawResponse(),
        (error) => error === caught,
      );
      await assert.rejects(
        response.finally(() => undefined),
        (error) => error === caught,
      );
      assert.equal(f.requests.length, 1);
      assert.equal(f.spans().length, 1);
      const [span] = await f.flush();
      assert.equal(span.status.code, 2);
      assert.equal(attributes(span)["http.response.status_code"], status);
      for (const marker of [
        INPUT,
        OUTPUT,
        KEY,
        "SIGNED_URL",
        "synthetic-error-body",
      ]) {
        // Default input is deliberately captured, but an error body must never become telemetry.
        if (marker !== INPUT) assert.ok(!JSON.stringify(span).includes(marker));
      }
      assert.equal(span.events.length, 0);
      assert.ok(!span.status.message);
    });

  test(`${name}: retry success emits one semantic span; timeout preserves SDK error`, async (t) => {
    const f = await run(t);
    const stream = await f.client.textToSpeech.convert("voice-id", request, {
      headers: { "x-case": "retry" },
      maxRetries: 1,
    });
    await bytes(stream);
    assert.equal(f.attempts.get("retry"), 2);
    assert.equal(f.spans().length, 1);
    await assert.rejects(
      f.client.textToSpeech.convert("voice-id", request, {
        ...opts("timeout"),
        timeoutInSeconds: 0.03,
      }),
      (error) =>
        error instanceof SDK.ElevenLabsError &&
        error.message.includes("timeout"),
    );
    assert.equal(f.spans().length, 2);
    assert.equal(f.spans()[1].attributes["elevenlabs.completion"], "error");
  });

  test(`${name}: pre-abort, mid-stream abort, reader cancel and early iterator return`, async (t) => {
    const f = await run(t);
    const pre = new AbortController();
    pre.abort();
    await assert.rejects(
      f.client.textToSpeech.convert("voice-id", request, {
        abortSignal: pre.signal,
        timeoutInSeconds: 0.05,
      }),
    );
    assert.equal(f.spans()[0].attributes["elevenlabs.completion"], "cancelled");
    const mid = new AbortController();
    const stream = await f.client.textToSpeech.stream("voice-id", request, {
      ...opts("slow"),
      abortSignal: mid.signal,
    });
    const reader = stream.getReader();
    await reader.read();
    mid.abort();
    await assert.rejects(reader.read());
    assert.equal(f.spans()[1].attributes["elevenlabs.completion"], "cancelled");
    const cancel = await f.client.textToSpeech.convert("voice-id", request);
    await cancel.getReader().cancel("synthetic-reason");
    const early = await f.client.textToSpeech.stream("voice-id", request);
    for await (const chunk of early) {
      assert.ok(chunk.length);
      break;
    }
    const timestamps = await f.client.textToSpeech.streamWithTimestamps(
      "voice-id",
      request,
    );
    for await (const chunk of timestamps) {
      assert.equal(chunk.audioBase64, BASE64);
      break;
    }
    assert.equal(f.spans().length, 5);
    assert.ok(
      f
        .spans()
        .every((s) => s.attributes["elevenlabs.completion"] === "cancelled"),
    );
  });

  test(`${name}: stream failures end once and retain application failure`, async (t) => {
    const f = await run(t);
    await assert.rejects(
      bytes(
        await f.client.textToSpeech.stream("voice-id", request, opts("broken")),
      ),
    );
    await assert.rejects(async () => {
      for await (const value of await f.client.textToSpeech.streamWithTimestamps(
        "voice-id",
        request,
        opts("bad-stream"),
      ))
        assert.equal(value.audioBase64, BASE64);
    });
    assert.equal(f.spans().length, 2);
    assert.ok(
      f.spans().every((s) => s.attributes["elevenlabs.completion"] === "error"),
    );
  });

  test(`${name}: abandoned span deadline and capacity never consume or cancel streams`, async (t) => {
    const f = await run(t, { maxSpanDurationMs: 60, maxActiveSpans: 1 });
    let reads = 0,
      cancels = 0;
    const raw = new SDK.ElevenLabsClient({
      apiKey: KEY,
      fetch: async () =>
        new Response(
          new ReadableStream(
            {
              pull(controller) {
                reads++;
                controller.enqueue(AUDIO);
              },
              cancel() {
                cancels++;
              },
            },
            { highWaterMark: 0 },
          ),
        ),
    });
    const client = f.instrumentation.wrapClient(raw);
    const unconsumed = await client.textToSpeech.convert("voice-id", request);
    const skipped = await client.textToSpeech.convert("voice-id", request);
    await delay(100);
    assert.equal(reads, 0);
    assert.equal(cancels, 0);
    assert.equal(f.spans().length, 1);
    assert.equal(
      f.spans()[0].attributes["elevenlabs.completion"],
      "incomplete",
    );
    assert.equal(f.spans()[0].status.code, 0);
    const reader = unconsumed.getReader();
    assert.deepEqual((await reader.read()).value, AUDIO);
    await reader.cancel();
    await skipped.cancel();
    assert.equal(
      f.spans().length,
      1,
      "late consumption cannot revise an incomplete span",
    );
    const abandoned = await client.textToSpeech.convert("voice-id", request);
    const abandonedReader = abandoned.getReader();
    await abandonedReader.read();
    const readsBefore = reads,
      cancelsBefore = cancels;
    f.instrumentation.closePendingSpans();
    assert.equal(reads, readsBefore);
    assert.equal(cancels, cancelsBefore);
    assert.equal(f.spans().length, 2);
    await abandonedReader.cancel();
  });

  test(`${name}: session hierarchy, concurrency, suppression, idempotence and disable`, async (t) => {
    const f = await run(t);
    assert.equal(f.instrumentation.wrapClient(f.client), f.client);
    const tracer = f.provider.getTracer("application");
    await Promise.all(
      [1, 2].map((i) =>
        tracer.startActiveSpan("parent-" + i, async (parent) => {
          const parentId = parent.spanContext().spanId;
          const scoped = setUser(
            setSession(context.active(), { sessionId: "session-" + i }),
            { userId: "user-" + i },
          );
          await context.with(scoped, async () => {
            await bytes(
              await f.client.textToSpeech.convert("voice-id", request),
            );
            const span = f
              .spans()
              .find((s) => s.parentSpanContext?.spanId === parentId);
            assert.ok(span);
            assert.equal(span.attributes["session.id"], "session-" + i);
            assert.equal(span.attributes["user.id"], "user-" + i);
          });
          parent.end();
        }),
      ),
    );
    assert.equal(f.spans().length, 2);
    await context.with(suppressTracing(context.active()), async () =>
      bytes(await f.client.textToSpeech.convert("voice-id", request)),
    );
    assert.equal(f.spans().length, 2);
    f.instrumentation.disable();
    await bytes(await f.client.textToSpeech.convert("voice-id", request));
    assert.equal(f.spans().length, 2);
    f.instrumentation.enable();
    await bytes(await f.client.textToSpeech.convert("voice-id", request));
    assert.equal(f.spans().length, 3);
    const wire = await f.flush();
    for (const span of wire.filter(
      (s) => s.name.startsWith("ElevenLabs") && s.parentSpanId,
    )) {
      const parent = wire.find((p) => p.spanId === span.parentSpanId);
      assert.ok(parent);
      assert.equal(span.traceId, parent.traceId);
    }
  });

  test(`${name}: manual module registration and restoring prototype descriptors`, async (t) => {
    const f = await run(t);
    const original = Object.getOwnPropertyDescriptor(
      SDK.ElevenLabsClient.prototype,
      "textToSpeech",
    );
    f.instrumentation.manuallyInstrument(SDK);
    f.instrumentation.manuallyInstrument(SDK);
    const raw = new SDK.ElevenLabsClient({ apiKey: KEY, baseUrl: f.baseUrl });
    await bytes(await raw.textToSpeech.convert("voice-id", request));
    assert.equal(f.spans().length, 1);
    await bytes(
      await f.instrumentation
        .wrapClient(raw)
        .textToSpeech.convert("voice-id", request),
    );
    assert.equal(
      f.spans().length,
      2,
      "module + explicit wrapper must not duplicate",
    );
    f.instrumentation.unpatch();
    assert.deepEqual(
      Object.getOwnPropertyDescriptor(
        SDK.ElevenLabsClient.prototype,
        "textToSpeech",
      ),
      original,
    );
    await bytes(await raw.textToSpeech.convert("voice-id", request));
    assert.equal(f.spans().length, 2);
  });

  test(`${name}: telemetry failure preserves responses, receivers and synchronous throws`, async (t) => {
    const f = await run(t);
    f.instrumentation.setTracerProvider({
      getTracer() {
        return {
          startSpan() {
            throw new Error("synthetic-telemetry-failure");
          },
        };
      },
    });
    await bytes(await f.client.textToSpeech.convert("voice-id", request));
    assert.equal(f.requests.length, 1);
    assert.equal(f.spans().length, 0);
    let calls = 0;
    const failure = new Error("original");
    const namespace = {
      convert() {
        calls++;
        assert.equal(this, namespace);
        throw failure;
      },
    };
    const wrapped = f.instrumentation.wrapClient({ textToSpeech: namespace });
    f.instrumentation.disable();
    assert.throws(
      () => wrapped.textToSpeech.convert("voice-id", request),
      (error) => error === failure,
    );
    assert.equal(calls, 1);
  });

  const privacy = [
    [],
    ["FI_HIDE_INPUTS"],
    ["FI_HIDE_OUTPUTS"],
    ["FI_HIDE_INPUT_MESSAGES"],
    ["FI_HIDE_OUTPUT_MESSAGES"],
    ["FI_HIDE_INPUT_TEXT"],
    ["FI_HIDE_OUTPUT_TEXT"],
    ["FI_HIDE_ERROR_DETAILS"],
    ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS"],
  ];
  for (const flags of privacy)
    test(`${name}: full OTLP privacy ${flags.join("+") || "core defaults"}`, async (t) => {
      const originalEnv = Object.fromEntries(
        flags.map((key) => [key, process.env[key]]),
      );
      for (const key of flags) process.env[key] = "true";
      t.after(() => {
        for (const [key, value] of Object.entries(originalEnv)) {
          if (value === undefined) delete process.env[key];
          else process.env[key] = value;
        }
      });
      const f = await run(t);
      await f.client.textToSpeech.convertWithTimestamps("voice-id", request);
      await f.client.speechToText.convert({
        modelId: "scribe_v2",
        cloudStorageUrl: "https://invalid.test/private.wav?token=SIGNED_URL",
      });
      await assert.rejects(
        f.client.textToSpeech.convert("voice-id", request, opts("error-500")),
      );
      const wire = JSON.stringify(await f.flush());
      const hiddenInput = flags.some((k) =>
        [
          "FI_HIDE_INPUTS",
          "FI_HIDE_INPUT_MESSAGES",
          "FI_HIDE_INPUT_TEXT",
        ].includes(k),
      );
      const hiddenOutput = flags.some((k) =>
        [
          "FI_HIDE_OUTPUTS",
          "FI_HIDE_OUTPUT_MESSAGES",
          "FI_HIDE_OUTPUT_TEXT",
        ].includes(k),
      );
      assert.equal(wire.includes(INPUT), !hiddenInput);
      assert.equal(wire.includes(OUTPUT), !hiddenOutput);
      for (const marker of [
        BASE64,
        KEY,
        "SIGNED_URL",
        "/private/",
        "synthetic-error-body",
        "synthetic-fi-secret",
        "exception.message",
        "exception.stacktrace",
      ])
        assert.ok(!wire.includes(marker), marker);
    });

  test(`${name}: text limits do not truncate customer responses`, async (t) => {
    const f = await run(t);
    await f.client.textToSpeech.convertWithTimestamps("voice-id", {
      ...request,
      text: "Y".repeat(10000),
    });
    const output = await f.client.speechToText.convert(
      { modelId: "scribe_v2", file: new Blob([AUDIO]) },
      opts("long"),
    );
    assert.equal(output.text.length, 10000);
    assert.equal(f.spans()[0].attributes["input.value"].length, 4096);
    assert.equal(f.spans()[1].attributes["output.value"].length, 4096);
  });
}
