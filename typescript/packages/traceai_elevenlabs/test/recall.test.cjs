const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, bytes, attributes, INPUT } = require("./fixture.cjs");
const cjs = require("@traceai/elevenlabs");
const methods = ["convert", "stream", "convertWithTimestamps", "streamWithTimestamps"];
const request = {
  text: "synthetic-discarded-text",
  modelId: "eleven_multilingual_v2",
  outputFormat: "mp3_44100_128",
};
async function consume(client, method, options) {
  const response = await client.textToSpeech[method]("voice-id", request, options);
  if (method === "convert" || method === "stream") await bytes(response);
  else if (method === "streamWithTimestamps")
    for await (const chunk of response) assert.ok(chunk.audioBase64);
}

for (const name of ["@elevenlabs/sdk-floor", "@elevenlabs/elevenlabs-js"]) {
  const SDK = require(name);
  for (const method of methods) {
    test(`${name} ${method}: capture effective body/query overrides only`, async (t) => {
      const f = await fixture(SDK);
      t.after(() => f.close());
      const options = {
        additionalBodyParameters: {
          text: "synthetic-actual-text",
          model_id: "eleven_flash_v2_5",
          private_data: "synthetic-unrecorded-body",
        },
        queryParams: { output_format: "pcm_16000", private_query: "synthetic-unrecorded-query" },
      };
      await consume(f.client, method, options);
      assert.equal(f.requests.length, 1);
      const body = JSON.parse(f.requests[0].body);
      const format = new URL(f.requests[0].url, f.baseUrl).searchParams.get("output_format");
      const spans = await f.flush();
      assert.equal(spans.length, 1);
      const attrs = attributes(spans[0]);
      assert.deepEqual(
        { input: attrs["input.value"], model: attrs["gen_ai.request.model"], format: attrs["elevenlabs.output_format"] },
        { input: body.text, model: body.model_id, format },
      );
      assert.ok(!JSON.stringify(f.wire).includes("synthetic-unrecorded"));
    });
  }
  for (const value of [null, undefined, ""]) {
    test(`${name}: body ${String(value)} overrides replace values; nullish query overrides are skipped`, async (t) => {
      const f = await fixture(SDK);
      t.after(() => f.close());
      await consume(f.client, "convert", {
        additionalBodyParameters: { text: value, model_id: value },
        queryParams: { output_format: value },
      });
      const body = JSON.parse(f.requests[0].body);
      const format = new URL(f.requests[0].url, f.baseUrl).searchParams.get("output_format");
      const [span] = await f.flush();
      const attrs = attributes(span);
      assert.equal(attrs["input.value"], typeof body.text === "string" ? body.text : undefined);
      assert.equal(attrs["gen_ai.request.model"], undefined);
      assert.equal(attrs["elevenlabs.output_format"], format || undefined);
    });
  }
  test(`${name}: inherited and non-enumerable overrides do not replace request fields`, async (t) => {
    const f = await fixture(SDK);
    t.after(() => f.close());
    const body = Object.create({ text: "ignored-text" });
    Object.defineProperty(body, "model_id", { value: "ignored-model" });
    const query = Object.create({ output_format: "ignored-format" });
    await consume(f.client, "convert", { additionalBodyParameters: body, queryParams: query });
    const [span] = await f.flush();
    const attrs = attributes(span);
    assert.equal(attrs["input.value"], request.text);
    assert.equal(attrs["gen_ai.request.model"], request.modelId);
    assert.equal(attrs["elevenlabs.output_format"], request.outputFormat);
  });
  for (const traceConfig of [{}, { hideInputs: true }, { hideInputMessages: true }, { hideInputText: true }]) {
    test(`${name}: effective values respect privacy and size limits ${JSON.stringify(traceConfig)}`, async (t) => {
      const f = await fixture(SDK, { traceConfig });
      t.after(() => f.close());
      await consume(f.client, "convert", {
        additionalBodyParameters: { text: "X".repeat(10000), model_id: "https://invalid.test/private-model" },
        queryParams: { output_format: "https://invalid.test/private-format" },
      });
      const [span] = await f.flush();
      const attrs = attributes(span);
      assert.equal(attrs["input.value"], Object.keys(traceConfig).length ? undefined : "X".repeat(4096));
      assert.equal(attrs["gen_ai.request.model"], undefined);
      assert.equal(attrs["elevenlabs.output_format"], undefined);
      assert.ok(!JSON.stringify(f.wire).includes("https://invalid.test"));
    });
  }
  for (const reverse of [false, true]) {
    test(`${name}: mixed public imports share ownership (${reverse ? "ESM first" : "CommonJS first"})`, async (t) => {
      const esm = await import("@traceai/elevenlabs");
      const f = await fixture(SDK);
      t.after(() => f.close());
      const First = reverse ? esm.ElevenLabsInstrumentation : cjs.ElevenLabsInstrumentation;
      const Second = reverse ? cjs.ElevenLabsInstrumentation : esm.ElevenLabsInstrumentation;
      const first = new First({ tracerProvider: f.provider });
      const second = new Second({ tracerProvider: f.provider });
      t.after(() => { first.shutdown(); second.shutdown(); });
      const plain = new SDK.ElevenLabsClient({ apiKey: "synthetic-key", baseUrl: f.baseUrl, maxRetries: 0 });
      const wrapped = first.wrapClient(plain);
      const client = second.wrapClient(wrapped);
      await bytes(await client.textToSpeech.convert("voice-id", { text: INPUT }));
      assert.equal(f.requests.length, 1);
      assert.equal((await f.flush()).length, 1);
      assert.equal(client, wrapped);
      assert.equal(esm.ElevenLabsInstrumentation, cjs.ElevenLabsInstrumentation);
      first.disable();
      const replacement = second.wrapClient(client);
      assert.notEqual(replacement, wrapped);
      await bytes(await replacement.textToSpeech.convert("voice-id", { text: INPUT }));
      assert.equal(f.requests.length, 2);
      assert.equal((await f.flush()).length, 2);
    });
  }
}
