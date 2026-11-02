const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, AUDIO, KEY } = require("./fixture.cjs");
const request = { text: "synthetic", modelId: "eleven_multilingual_v2" };

for (const name of ["@elevenlabs/sdk-floor", "@elevenlabs/elevenlabs-js"]) {
  const SDK = require(name);
  async function run(t) {
    const f = await fixture(SDK);
    t.after(() => f.close());
    return f;
  }
  function client(f, source, traced = true) {
    const raw = new SDK.ElevenLabsClient({
      apiKey: KEY,
      fetch: async () => new Response(source),
    });
    return traced ? f.instrumentation.wrapClient(raw) : raw;
  }

  for (const method of ["convert", "stream"]) {
    test(
      `${name}: ${method} forwards caller BYOB views, partial fills and EOF`,
      { timeout: 10000 },
      async (t) => {
        const f = await run(t);
        for (const audio of [Buffer.alloc(0), AUDIO]) {
          const results = [];
          for (const traced of [false, true]) {
            let offset = 0;
            const views = [];
            const source = new ReadableStream({
              type: "bytes",
              pull(controller) {
                const byob = controller.byobRequest;
                assert.ok(byob, "upstream must receive the caller's BYOB view");
                const view = byob.view;
                views.push([
                  view.byteOffset,
                  view.byteLength,
                  view.buffer.byteLength,
                ]);
                if (offset === audio.length) {
                  controller.close();
                  byob.respond(0);
                } else {
                  const count = Math.min(
                    2,
                    view.byteLength,
                    audio.length - offset,
                  );
                  view.set(audio.subarray(offset, offset + count));
                  offset += count;
                  byob.respond(count);
                }
              },
            });
            const output = await client(f, source, traced).textToSpeech[method](
              "voice-id",
              request,
            );
            assert.equal(views.length, 0, "no reads before the consumer asks");
            const reader = output.getReader({ mode: "byob" });
            const chunks = [];
            for (;;) {
              const buffer = new ArrayBuffer(10);
              new Uint8Array(buffer).fill(91);
              const pending = reader.read(new Uint8Array(buffer, 2, 3));
              assert.equal(
                buffer.byteLength,
                0,
                "retain native buffer transfer",
              );
              const next = await pending;
              assert.equal(next.value.byteOffset, 2);
              assert.equal(next.value.buffer.byteLength, 10);
              assert.equal(new Uint8Array(next.value.buffer)[0], 91);
              assert.equal(new Uint8Array(next.value.buffer)[9], 91);
              if (next.done) {
                assert.equal(next.value.byteLength, 0);
                break;
              }
              chunks.push(Buffer.from(next.value));
            }
            reader.releaseLock();
            assert.equal(source.locked, false);
            assert.deepEqual(Buffer.concat(chunks), audio);
            assert.ok(views.every((view) => view.join() === "2,3,10"));
            results.push({ chunks, views });
          }
          assert.deepEqual(
            results[1],
            results[0],
            "wrapped and native reads agree",
          );
          const span = f.spans().at(-1);
          assert.equal(span.attributes["elevenlabs.completion"], "complete");
          assert.equal(
            span.attributes["elevenlabs.audio.bytes"],
            audio.length || undefined,
          );
        }
        assert.equal(
          f.spans().length,
          2,
          "exactly one span per wrapped request",
        );
        assert.equal(
          (await f.flush()).length,
          2,
          "exactly one OTLP span per wrapped request",
        );
      },
    );
  }

  test(
    `${name}: BYOB typed views and minimum fill retain partial final data`,
    { timeout: 10000 },
    async (t) => {
      const f = await run(t);
      const audio = AUDIO.subarray(0, 6);
      const results = [];
      for (const traced of [false, true]) {
        let offset = 0;
        const source = new ReadableStream({
          type: "bytes",
          pull(controller) {
            const byob = controller.byobRequest;
            assert.ok(byob);
            byob.view.set(audio.subarray(offset, offset + 2));
            offset += 2;
            byob.respond(2);
            if (offset === audio.length) {
              controller.close();
              controller.byobRequest?.respond(0);
            }
          },
        });
        const output = await client(f, source, traced).textToSpeech.convert(
          "voice-id",
          request,
        );
        const reader = output.getReader({ mode: "byob" });
        const next = await reader.read(
          new Uint16Array(new ArrayBuffer(12), 2, 4),
          { min: 4 },
        );
        assert.ok(next.value instanceof Uint16Array);
        assert.equal(next.value.byteOffset, 2);
        assert.deepEqual(
          Buffer.from(
            next.value.buffer,
            next.value.byteOffset,
            next.value.byteLength,
          ),
          audio,
        );
        results.push(next);
        assert.equal((await reader.read(new Uint16Array(4))).done, true);
        reader.releaseLock();
        assert.equal(source.locked, false);
      }
      assert.deepEqual(results[1], results[0]);
      assert.equal(f.spans().length, 1);
      assert.equal(f.spans()[0].attributes["elevenlabs.audio.bytes"], 6);
      assert.equal(
        f.spans()[0].attributes["elevenlabs.completion"],
        "complete",
      );
    },
  );

  test(
    `${name}: upstream reader mode follows default/BYOB/default/BYOB consumers`,
    { timeout: 10000 },
    async (t) => {
      const f = await run(t);
      const modes = [];
      const source = new ReadableStream({
        type: "bytes",
        pull(controller) {
          const byob = controller.byobRequest;
          modes.push(Boolean(byob));
          if (modes.length === 4) {
            controller.close();
            byob?.respond(0);
          } else if (byob) {
            byob.view[0] = modes.length;
            byob.respond(1);
          } else controller.enqueue(Uint8Array.of(modes.length));
        },
      });
      const output = await client(f, source).textToSpeech.stream(
        "voice-id",
        request,
      );
      for (const [index, byob] of [false, true, false, true].entries()) {
        const reader = output.getReader(byob ? { mode: "byob" } : undefined);
        const next = await reader.read(...(byob ? [new Uint8Array(8)] : []));
        assert.equal(next.done, index === 3);
        if (!next.done) assert.deepEqual([...next.value], [index + 1]);
        reader.releaseLock();
      }
      assert.deepEqual(modes, [false, true, false, true]);
      assert.equal(source.locked, false);
      assert.equal(f.spans().length, 1);
      assert.equal(f.spans()[0].attributes["elevenlabs.audio.bytes"], 3);
    },
  );

  test(
    `${name}: pending BYOB cancellation propagates once and releases its reader`,
    { timeout: 10000 },
    async (t) => {
      const f = await run(t);
      let entered;
      const started = new Promise((resolve) => {
        entered = resolve;
      });
      const reasons = [];
      const source = new ReadableStream({
        type: "bytes",
        pull(controller) {
          entered(Boolean(controller.byobRequest));
        },
        cancel(reason) {
          reasons.push(reason);
        },
      });
      const output = await client(f, source).textToSpeech.stream(
        "voice-id",
        request,
      );
      const reader = output.getReader({ mode: "byob" });
      const pending = reader.read(new Uint8Array(8));
      const sawBYOB = await started;
      const reason = { synthetic: "cancel" };
      await reader.cancel(reason);
      assert.deepEqual(await pending, { done: true, value: undefined });
      assert.equal(sawBYOB, true);
      assert.deepEqual(reasons, [reason]);
      assert.equal(source.locked, false);
      assert.equal(f.spans().length, 1);
      assert.equal(
        f.spans()[0].attributes["elevenlabs.completion"],
        "cancelled",
      );
    },
  );

  test(
    `${name}: BYOB source rejection preserves error identity and releases its reader`,
    { timeout: 10000 },
    async (t) => {
      const f = await run(t);
      const error = new Error("synthetic BYOB failure");
      let sawBYOB;
      const source = new ReadableStream({
        type: "bytes",
        pull(controller) {
          sawBYOB = Boolean(controller.byobRequest);
          throw error;
        },
      });
      const output = await client(f, source).textToSpeech.convert(
        "voice-id",
        request,
      );
      const reader = output.getReader({ mode: "byob" });
      await assert.rejects(
        reader.read(new Uint8Array(8)),
        (actual) => actual === error,
      );
      assert.equal(sawBYOB, true);
      assert.equal(source.locked, false);
      assert.equal(f.spans().length, 1);
      assert.equal(f.spans()[0].attributes["elevenlabs.completion"], "error");
    },
  );
}
