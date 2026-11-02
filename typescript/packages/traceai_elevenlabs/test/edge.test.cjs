const { diag, DiagLogLevel } = require("@opentelemetry/api");
const diagnostics = [];
diag.setLogger(
  {
    error() {},
    warn(message) {
      diagnostics.push(message);
    },
    info() {},
    debug() {},
    verbose() {},
  },
  DiagLogLevel.ALL,
);
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { fixture, bytes, AUDIO, BASE64, KEY, delay } = require("./fixture.cjs");
const { ElevenLabsInstrumentation } = require(
  process.env.TRACEAI_PACKAGE || "../dist/src",
);
const request = { text: "synthetic", modelId: "eleven_multilingual_v2" };
for (const name of ["@elevenlabs/sdk-floor", "@elevenlabs/elevenlabs-js"]) {
  const SDK = require(name);
  async function run(t, options) {
    const f = await fixture(SDK, options);
    t.after(() => f.close());
    return f;
  }
  test(`${name}: empty audio and malformed SDK requests retain their outcomes`, async (t) => {
    const f = await run(t);
    let calls = 0;
    const raw = new SDK.ElevenLabsClient({
      apiKey: KEY,
      fetch: async () => {
        calls++;
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.close();
            },
          }),
        );
      },
    });
    const client = f.instrumentation.wrapClient(raw);
    assert.deepEqual(
      await bytes(await client.textToSpeech.convert("voice-id", request)),
      Buffer.alloc(0),
    );
    assert.equal(f.spans()[0].attributes["elevenlabs.completion"], "complete");
    assert.equal(
      f.spans()[0].attributes["elevenlabs.first_audio_latency_ms"],
      undefined,
    );
    let original;
    await assert.rejects(
      raw.textToSpeech.convert("voice-id", undefined),
      (error) => {
        original = error;
        return true;
      },
    );
    await assert.rejects(
      client.textToSpeech.convert("voice-id", undefined),
      (error) =>
        error.constructor === original.constructor &&
        error.message === original.message,
    );
    assert.equal(calls, 1);
    assert.equal(f.spans().length, 2);
    assert.equal(f.spans()[1].attributes["elevenlabs.completion"], "error");
  });
  test(`${name}: BYOB readers retain native byte-stream support`, async (t) => {
    const f = await run(t);
    let reads = 0;
    const source = new ReadableStream({
      type: "bytes",
      pull(controller) {
        reads++;
        if (reads === 1) controller.enqueue(Uint8Array.from(AUDIO));
        else {
          controller.close();
          controller.byobRequest?.respond(0);
        }
      },
    });
    const raw = new SDK.ElevenLabsClient({
      apiKey: KEY,
      fetch: async () => new Response(source),
    });
    const audio = await f.instrumentation
      .wrapClient(raw)
      .textToSpeech.stream("voice-id", request);
    assert.equal(reads, 0);
    const reader = audio.getReader({ mode: "byob" });
    const chunks = [];
    for (;;) {
      const next = await reader.read(new Uint8Array(3));
      if (next.done) break;
      chunks.push(Buffer.from(next.value));
    }
    assert.deepEqual(Buffer.concat(chunks), AUDIO);
    assert.equal(f.spans()[0].attributes["elevenlabs.completion"], "complete");
  });
  test(`${name}: preventCancel and pipeThrough preserve stream behavior`, async (t) => {
    const f = await run(t, { maxSpanDurationMs: 1000 });
    const audio = await f.client.textToSpeech.stream("voice-id", request, {
      headers: { "x-case": "slow" },
    });
    for await (const chunk of audio.values({ preventCancel: true })) {
      assert.ok(chunk.length);
      break;
    }
    assert.equal(
      f.spans().length,
      0,
      "releasing a reader is not EOF or cancellation",
    );
    assert.ok((await bytes(audio)).length > 0);
    assert.equal(f.spans().length, 1);
    const second = await f.client.textToSpeech.stream("voice-id", request);
    assert.deepEqual(
      await bytes(second.pipeThrough(new TransformStream())),
      AUDIO,
    );
    assert.equal(f.spans().length, 2);
  });
  test(`${name}: disabled owner can be replaced without nested instrumentation`, async (t) => {
    const f = await run(t);
    const other = new ElevenLabsInstrumentation({ tracerProvider: f.provider });
    t.after(() => other.shutdown());
    assert.equal(other.wrapClient(f.client), f.client);
    await bytes(await f.client.textToSpeech.convert("voice-id", request));
    assert.equal(f.spans().length, 1);
    f.instrumentation.disable();
    const replaced = other.wrapClient(f.client);
    await bytes(await replaced.textToSpeech.convert("voice-id", request));
    assert.equal(f.spans().length, 2);
    assert.equal(
      f.spans().at(-1).attributes["elevenlabs.completion"],
      "complete",
    );
  });
  test(`${name}: span method failures never break successful SDK operations`, async (t) => {
    const f = await run(t);
    const original = f.provider.getTracer("failure-test");
    f.instrumentation.setTracerProvider({
      getTracer() {
        return {
          startSpan(...args) {
            const span = original.startSpan(...args);
            return new Proxy(span, {
              get(target, key) {
                if (
                  ["setAttribute", "setAttributes", "setStatus"].includes(key)
                )
                  return () => {
                    throw new Error("synthetic telemetry failure");
                  };
                const value = Reflect.get(target, key, target);
                return typeof value === "function" ? value.bind(target) : value;
              },
            });
          },
        };
      },
    });
    assert.deepEqual(
      await bytes(await f.client.textToSpeech.stream("voice-id", request)),
      AUDIO,
    );
    assert.equal(f.requests.length, 1);
    assert.equal(f.memory.getFinishedSpans().length, 1);
    assert.ok(
      diagnostics.some((message) =>
        message.includes("@traceai/elevenlabs: telemetry failed"),
      ),
    );
    assert.ok(
      !JSON.stringify(diagnostics).includes("synthetic telemetry failure"),
    );
  });
  test(`${name}: abandoned unref timer does not keep a consumer alive`, () => {
    const result = spawnSync(
      process.execPath,
      [
        "-e",
        `
      const {ElevenLabsClient} = require(${JSON.stringify(name)});
      const {ElevenLabsInstrumentation} = require(${JSON.stringify(process.env.TRACEAI_PACKAGE || "../dist/src")});
      const {BasicTracerProvider} = require('@opentelemetry/sdk-trace-base');
      (async () => {
        const provider = new BasicTracerProvider();
        const instrumentation = new ElevenLabsInstrumentation({tracerProvider: provider});
        const client = instrumentation.wrapClient(new ElevenLabsClient({apiKey:'synthetic', fetch:async () => new Response(new ReadableStream({pull(){}}))}));
        await client.textToSpeech.convert('voice-id', {text:'synthetic'});
        await provider.shutdown();
      })().catch(() => process.exitCode=1);
    `,
      ],
      { cwd: __dirname, timeout: 60000, encoding: "utf8" },
    );
    assert.equal(result.status, 0, result.stderr || String(result.error));
  });
  if (name === "@elevenlabs/elevenlabs-js")
    test(`${name}: withMetadata timestamp stream stays lazy and lossless`, async (t) => {
      const f = await run(t);
      const stream = await f.client.textToSpeech.streamWithTimestamps(
        "voice-id",
        request,
      );
      assert.equal(typeof stream.withMetadata, "function");
      await delay(20);
      assert.equal(f.spans().length, 0);
      const chunks = [];
      for await (const event of stream.withMetadata()) chunks.push(event.data);
      assert.equal(chunks.length, 2);
      assert.equal(chunks[0].audioBase64, BASE64);
      assert.equal(f.spans().length, 1);
      assert.equal(f.spans()[0].attributes["elevenlabs.audio.chunks"], 2);
    });
}
