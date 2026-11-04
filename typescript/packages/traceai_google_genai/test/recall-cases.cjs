"use strict";
// Each case gets an isolated tracer, SDK prototype and privacy environment.
// Only synthetic inputs and mock fetch are used; run under the network sandbox.
const assert = require("node:assert/strict");
const path = require("node:path");
const { createRequire } = require("node:module");
const consumer = createRequire(path.join(process.argv[2], "package.json"));
const scenario = process.argv[3];
let google =
  scenario === "auto-lifecycle" ? undefined : consumer("@google/generative-ai");
const sdkVersion = consumer("@google/generative-ai/package.json").version;
const { GoogleGenAIInstrumentation, isPatched } = consumer(
  "@traceai/google-genai",
);
const { NodeTracerProvider } = consumer("@opentelemetry/sdk-trace-node");
const { InMemorySpanExporter, SimpleSpanProcessor } = consumer(
  "@opentelemetry/sdk-trace-base",
);
const { trace, context } = consumer("@opentelemetry/api");
const { suppressTracing } = consumer("@opentelemetry/core");
const { SemanticConventions: S } = consumer("@traceai/fi-semantic-conventions");
const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
provider.register();
const requests = [];
const contexts = [];
const answer = {
  candidates: [
    {
      content: { role: "model", parts: [{ text: "synthetic-answer-PRIVATE" }] },
      finishReason: "STOP",
    },
  ],
  usageMetadata: {
    promptTokenCount: 5,
    candidatesTokenCount: 2,
    totalTokenCount: 7,
  },
  modelVersion: "synthetic-model-resolved-001",
};
let response = answer;
global.fetch = async (url, options) => {
  requests.push({ url: String(url), body: JSON.parse(options.body) });
  contexts.push(trace.getActiveSpan()?.spanContext().spanId);
  const op = new URL(url).pathname;
  const body = op.endsWith(":batchEmbedContents")
    ? {
        embeddings: JSON.parse(options.body).requests.map(() => ({
          values: [0.123456789, 0.987654321],
        })),
      }
    : op.endsWith(":embedContent")
      ? { embedding: { values: [0.123456789, 0.987654321] } }
      : response;
  return new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json" },
  });
};
const inst = new GoogleGenAIInstrumentation({
  instrumentationConfig: { enabled: true },
});
inst.setTracerProvider(provider);
google ??= consumer("@google/generative-ai");
const client = new google.GoogleGenerativeAI("synthetic-google-key-PRIVATE");
const model = client.getGenerativeModel({ model: "synthetic-model" });
function patch() {
  inst.manuallyInstrument(google);
}
function attributes() {
  return exporter.getFinishedSpans().map((s) => s.attributes);
}
function spans() {
  return exporter.getFinishedSpans().map((s) => ({
    name: s.name,
    kind: s.kind,
    id: s.spanContext().spanId,
    parent: s.parentSpanContext,
    status: s.status,
    attributes: s.attributes,
    events: s.events,
  }));
}
async function outcome(f) {
  try {
    return { resolved: true, value: await f() };
  } catch (e) {
    return { resolved: false, name: e.name, message: e.message };
  }
}
function occurrences(marker) {
  return attributes().flatMap((a, i) =>
    Object.entries(a)
      .filter(([, v]) => JSON.stringify(v).includes(marker))
      .map(([key]) => ({ span: i, key })),
  );
}
async function run() {
  if (scenario === "auto-lifecycle") {
    assert.equal(isPatched(), true);
    await model.generateContent("auto");
    patch();
    await model.generateContent("auto-plus-manual");
    assert.equal(attributes().length, 2);
    inst.disable();
    assert.equal(isPatched(), false);
    await model.generateContent("off");
    assert.equal(attributes().length, 2);
    inst.enable();
    await model.generateContent("on");
    assert.equal(attributes().length, 3);
    inst.disable();
    return {};
  }
  if (scenario === "esm-lifecycle") {
    inst.disable();
    const { pathToFileURL } = require("node:url");
    const esm = await import(
      pathToFileURL(
        path.join(
          path.dirname(consumer.resolve("@google/generative-ai")),
          "index.mjs",
        ),
      ).href
    );
    const adapter = await import(
      pathToFileURL(
        path.resolve(
          path.dirname(consumer.resolve("@traceai/google-genai")),
          "../esm/index.js",
        ),
      ).href
    );
    const original = esm.GenerativeModel.prototype.generateContent;
    const esmInst = new adapter.GoogleGenAIInstrumentation({
      instrumentationConfig: { enabled: false },
    });
    esmInst.setTracerProvider(provider);
    esmInst.manuallyInstrument(esm);
    esmInst.manuallyInstrument(esm);
    const esmModel = new esm.GoogleGenerativeAI(
      "synthetic-esm-key",
    ).getGenerativeModel({ model: "synthetic-model" });
    assert.equal(
      (await esmModel.generateContent(["one", "two"])).response.text(),
      "synthetic-answer-PRIVATE",
    );
    assert.equal(attributes().length, 1);
    esmInst.disable();
    assert.equal(adapter.isPatched(), false);
    assert.equal(esm.GenerativeModel.prototype.generateContent, original);
    await esmModel.generateContent("disabled");
    assert.equal(attributes().length, 1);
    return {};
  }
  if (scenario === "array-overloads") {
    const inputs = [
      ["synthetic-a", "synthetic-b"],
      [
        { text: "describe" },
        { inlineData: { mimeType: "image/png", data: "AAAA" } },
      ],
    ];
    const off = [];
    for (const input of inputs)
      off.push(
        await outcome(() =>
          model.generateContent(input).then((r) => r.response.text()),
        ),
      );
    patch();
    const on = [];
    for (const input of inputs)
      on.push(
        await outcome(() =>
          model.generateContent(input).then((r) => r.response.text()),
        ),
      );
    return { off, on, requests: requests.length };
  }
  if (scenario === "blocked-responses") {
    const bodies = [
      { candidates: [{ finishReason: "SAFETY", safetyRatings: [] }] },
      { promptFeedback: { blockReason: "SAFETY", safetyRatings: [] } },
      { candidates: [{ finishReason: "MAX_TOKENS" }] },
    ];
    const off = [];
    for (const body of bodies) {
      response = body;
      off.push(
        await outcome(() =>
          model
            .generateContent("safe-request")
            .then((r) => JSON.parse(JSON.stringify(r.response))),
        ),
      );
    }
    patch();
    const on = [];
    for (const body of bodies) {
      response = body;
      on.push(
        await outcome(() =>
          model
            .generateContent("safe-request")
            .then((r) => JSON.parse(JSON.stringify(r.response))),
        ),
      );
    }
    return { off, on, spans: spans() };
  }
  if (scenario === "privacy-embedding-input") {
    process.env.FI_HIDE_INPUTS = "true";
    inst.enable();
    patch();
    await model.embedContent("synthetic-embedding-input-PRIVATE");
    await model.batchEmbedContents({
      requests: [0, 1].map((i) => ({
        content: {
          role: "user",
          parts: [{ text: `synthetic-embedding-input-PRIVATE-${i}` }],
        },
      })),
    });
    return {
      leaks: occurrences("synthetic-embedding-input-PRIVATE"),
      attributes: attributes(),
    };
  }
  if (scenario === "privacy-embedding-vector") {
    process.env.FI_HIDE_EMBEDDING_VECTORS = "true";
    inst.enable();
    patch();
    await model.embedContent("synthetic-input");
    await model.batchEmbedContents({
      requests: [0, 1].map(() => ({
        content: { role: "user", parts: [{ text: "synthetic-input" }] },
      })),
    });
    return { leaks: occurrences("0.123456789"), attributes: attributes() };
  }
  if (scenario === "privacy-text") {
    process.env.FI_HIDE_INPUT_TEXT = "true";
    process.env.FI_HIDE_OUTPUT_TEXT = "true";
    inst.enable();
    patch();
    await model.generateContent("synthetic-prompt-PRIVATE");
    return {
      inputLeaks: occurrences("synthetic-prompt-PRIVATE"),
      outputLeaks: occurrences("synthetic-answer-PRIVATE"),
      attributes: attributes(),
    };
  }
  if (scenario === "privacy-image") {
    process.env.FI_HIDE_INPUT_IMAGES = "true";
    inst.enable();
    patch();
    await model.generateContent({
      contents: [
        {
          role: "user",
          parts: [
            { text: "describe" },
            {
              inlineData: {
                mimeType: "image/png",
                data: "SYNTHETIC_IMAGE_PRIVATE",
              },
            },
          ],
        },
      ],
    });
    return {
      leaks: occurrences("SYNTHETIC_IMAGE_PRIVATE"),
      attributes: attributes(),
    };
  }
  if (scenario === "model-defaults") {
    const configured = client.getGenerativeModel({
      model: "synthetic-model",
      generationConfig: { temperature: 0.17, maxOutputTokens: 31 },
      systemInstruction: "synthetic-system-instruction",
      tools: [
        {
          functionDeclarations: [
            { name: "get_weather", description: "Get weather" },
          ],
        },
      ],
    });
    patch();
    await configured.generateContent("synthetic-user-message");
    return { wire: requests[0].body, attributes: attributes()[0] };
  }
  if (scenario === "request-system-string") {
    patch();
    await model.generateContent({
      contents: [{ role: "user", parts: [{ text: "hello" }] }],
      systemInstruction: "synthetic-system-instruction",
    });
    return { wire: requests[0].body, attributes: attributes()[0] };
  }
  if (scenario === "response-model") {
    patch();
    await model.generateContent("hello");
    return {
      responseModel: answer.modelVersion,
      attribute: attributes()[0][S.GEN_AI_RESPONSE_MODEL],
    };
  }
  if (scenario === "embedding-overloads") {
    patch();
    await model.embedContent(["synthetic-first", "synthetic-second"]);
    await model.embedContent([
      { text: "synthetic-first" },
      { text: "synthetic-second" },
    ]);
    await model.embedContent({
      content: {
        role: "user",
        parts: [{ text: "synthetic-first" }, { text: "synthetic-second" }],
      },
    });
    await model.batchEmbedContents({
      requests: [
        {
          content: {
            role: "user",
            parts: [{ text: "synthetic-first" }, { text: "synthetic-second" }],
          },
        },
      ],
    });
    return { wire: requests.map((r) => r.body), attributes: attributes() };
  }
  if (scenario === "disable-manual") {
    const original = google.GenerativeModel.prototype.generateContent;
    patch();
    await model.generateContent("before-disable");
    inst.disable();
    const restored =
      google.GenerativeModel.prototype.generateContent === original;
    await model.generateContent("after-disable");
    return {
      restored,
      patched: isPatched(),
      enabled: inst.isEnabled(),
      spanCount: attributes().length,
    };
  }
  if (scenario === "multiple-sdk-copies") {
    const other = consumer("google-generative-ai-other");
    assert.notEqual(other.GenerativeModel, google.GenerativeModel);
    patch();
    inst.manuallyInstrument(other);
    await model.generateContent("first-copy");
    await new other.GoogleGenerativeAI("synthetic-second-key")
      .getGenerativeModel({ model: "synthetic-model" })
      .generateContent("second-copy");
    return { requests: requests.length, spanCount: attributes().length };
  }
  if (scenario === "parent-suppression-duplicate") {
    patch();
    patch();
    const tracer = provider.getTracer("probe");
    const parent = tracer.startSpan("parent");
    await context.with(trace.setSpan(context.active(), parent), () =>
      model.generateContent("child"),
    );
    parent.end();
    await context.with(suppressTracing(context.active()), () =>
      model.generateContent("suppressed"),
    );
    return { spans: spans(), contexts };
  }
  if (scenario === "cancel-timeout") {
    patch();
    global.fetch = async (url, options) =>
      new Promise((resolve, reject) => {
        const abort = () => reject(new Error("synthetic-aborted-PRIVATE"));
        if (options.signal?.aborted) abort();
        else options.signal?.addEventListener("abort", abort, { once: true });
      });
    const controller = new AbortController();
    const pending = model.generateContent("synthetic-prompt", {
      signal: controller.signal,
    });
    setTimeout(() => controller.abort(), 1);
    const cancelled = await outcome(() => pending);
    const timedout = await outcome(() =>
      model.generateContent("synthetic-prompt", { timeout: 1 }),
    );
    return { cancelled, timedout, spans: spans() };
  }
  if (scenario === "hidden-errors") {
    process.env.FI_HIDE_ERROR_DETAILS = "true";
    inst.enable();
    patch();
    global.fetch = async () => {
      throw new Error("synthetic-sensitive-error-PRIVATE");
    };
    const result = await outcome(() => model.generateContent("hello"));
    return { result, spans: spans() };
  }
  if (scenario === "privacy-options") {
    const inputMarker = "synthetic-input-PRIVATE";
    const outputMarker = "synthetic-answer-PRIVATE";
    const imageMarker = "SYNTHETIC_IMAGE_PRIVATE";
    const flags = {
      hideInputs: inputMarker,
      hideInputText: inputMarker,
      hideInputMessages: inputMarker,
      hideInputImages: imageMarker,
      hideOutputs: outputMarker,
      hideOutputText: outputMarker,
      hideOutputMessages: outputMarker,
      hideEmbeddingVectors: "0.123456789",
    };
    const prompt = {
      contents: [
        {
          role: "user",
          parts: [
            { text: inputMarker },
            { inlineData: { mimeType: "image/png", data: imageMarker } },
          ],
        },
      ],
    };
    const document = {
      content: {
        role: "user",
        parts: [
          { text: inputMarker + "-first" },
          { text: inputMarker + "-second" },
        ],
      },
    };
    const before = JSON.stringify([prompt, document]);
    const checked = [];
    inst.disable();
    for (const [flag, marker] of Object.entries(flags)) {
      const envKey =
        "FI_" + flag.replace(/[A-Z]/g, (c) => "_" + c).toUpperCase();
      // Constructor options must win over contradictory environment values.
      for (const explicit of [true, false]) {
        process.env[envKey] = String(!explicit);
        const privacyInst = new GoogleGenAIInstrumentation({
          instrumentationConfig: { enabled: false },
          traceConfig: { [flag]: explicit },
        });
        privacyInst.setTracerProvider(provider);
        privacyInst.manuallyInstrument(google);
        exporter.reset();
        requests.length = 0;
        const generated = await model.generateContent(prompt);
        const embedded = await model.embedContent(document);
        const batch = await model.batchEmbedContents({
          requests: [document, document],
        });
        assert.equal(generated.response.text(), outputMarker);
        assert.deepEqual(embedded.embedding.values, [0.123456789, 0.987654321]);
        assert.equal(batch.embeddings.length, 2);
        assert.equal(JSON.stringify([prompt, document]), before);
        assert.deepEqual(requests[0].body.contents, prompt.contents);
        assert.deepEqual(requests[1].body.content, document.content);
        assert.equal(requests[2].body.requests.length, 2);
        assert.equal(attributes().length, 3);
        if (explicit) {
          assert.deepEqual(occurrences(marker), [], flag);
          if (flag === "hideOutputs")
            assert.deepEqual(occurrences("0.123456789"), [], flag);
        } else
          assert.ok(
            occurrences(marker).length,
            flag + " explicit false should win",
          );
        privacyInst.disable();
        delete process.env[envKey];
        checked.push({ flag, explicit });
      }
    }
    return { checked };
  }
  if (scenario === "model-overrides") {
    const defaults = { temperature: 0.17, maxOutputTokens: 31 };
    const configured = client.getGenerativeModel({
      model: "synthetic-model",
      generationConfig: defaults,
      systemInstruction: "synthetic-default-system",
      tools: [{ functionDeclarations: [{ name: "default_tool" }] }],
    });
    const request = {
      contents: [{ role: "user", parts: [{ text: "override" }] }],
      generationConfig: { temperature: 0.91 },
      tools: [],
    };
    const before = JSON.stringify({ defaults, request });
    patch();
    await configured.generateContent(request);
    const attr = attributes()[0];
    assert.deepEqual(
      JSON.parse(attr[S.LLM_INVOCATION_PARAMETERS]),
      requests[0].body.generationConfig,
    );
    assert.deepEqual(requests[0].body.generationConfig, { temperature: 0.91 });
    assert.equal(attr[S.LLM_TOOLS], undefined);
    assert.equal(JSON.stringify({ defaults, request }), before);
    await configured.generateContent({
      contents: request.contents,
      generationConfig: undefined,
    });
    assert.equal(attributes()[1][S.LLM_INVOCATION_PARAMETERS], undefined);
    return { attributes: attributes() };
  }
  if (scenario === "missing-response-model") {
    response = { ...answer };
    delete response.modelVersion;
    patch();
    await model.generateContent("hello");
    assert.equal(attributes()[0][S.GEN_AI_RESPONSE_MODEL], undefined);
    return {};
  }
  if (scenario === "lifecycle-ownership") {
    const originals = Object.fromEntries(
      ["generateContent", "embedContent", "batchEmbedContents"].map((m) => [
        m,
        google.GenerativeModel.prototype[m],
      ]),
    );
    patch();
    inst.manuallyInstrument(
      Object.freeze({ GenerativeModel: google.GenerativeModel }),
    );
    const wrapped = google.GenerativeModel.prototype.generateContent;
    const observer = new GoogleGenAIInstrumentation({
      instrumentationConfig: { enabled: false },
    });
    observer.setTracerProvider(provider);
    observer.manuallyInstrument(google);
    observer.disable();
    assert.equal(google.GenerativeModel.prototype.generateContent, wrapped);
    await model.generateContent("one");
    assert.equal(attributes().length, 1);
    inst.disable();
    inst.disable();
    assert.equal(isPatched(), false);
    for (const [method, original] of Object.entries(originals))
      assert.equal(google.GenerativeModel.prototype[method], original);
    await model.generateContent("not-traced");
    assert.equal(attributes().length, 1);
    inst.enable();
    inst.enable();
    await model.generateContent("two");
    assert.equal(attributes().length, 2);
    assert.equal(isPatched(), true);
    inst.disable();
    assert.equal(isPatched(), false);
    return {};
  }
  if (scenario === "extraction-failures") {
    const sentinel = {
      response: {
        text() {
          throw new Error("synthetic-helper-throw");
        },
        candidates: [{ finishReason: "SAFETY" }],
      },
    };
    const invalidForTelemetry = {
      contents: {
        forEach() {
          throw new Error("synthetic-extraction-throw");
        },
      },
    };
    const options = { synthetic: "options" };
    const seen = [];
    const rejection = new Error("synthetic-rejection");
    const syncError = new Error("synthetic-sync");
    class Model {
      generateContent(...args) {
        seen.push({ self: this, args });
        if (args[0] === "reject") return Promise.reject(rejection);
        if (args[0] === "throw") throw syncError;
        return Promise.resolve(sentinel);
      }
      embedContent() {
        return Promise.resolve({ embedding: undefined });
      }
      batchEmbedContents() {
        return Promise.resolve({ embeddings: undefined });
      }
    }
    const instance = new Model();
    inst.manuallyInstrument({ GenerativeModel: Model });
    assert.equal(
      await instance.generateContent(invalidForTelemetry, options),
      sentinel,
    );
    assert.equal(seen[0].self, instance);
    assert.equal(seen[0].args[0], invalidForTelemetry);
    assert.equal(seen[0].args[1], options);
    assert.deepEqual(await instance.embedContent(null), {
      embedding: undefined,
    });
    assert.deepEqual(await instance.batchEmbedContents(null), {
      embeddings: undefined,
    });
    await assert.rejects(
      instance.generateContent("reject"),
      (error) => error === rejection,
    );
    assert.throws(
      () => instance.generateContent("throw"),
      (error) => error === syncError,
    );
    assert.equal(attributes().length, 5);
    assert.deepEqual(
      spans().map((s) => s.status.code),
      [1, 1, 1, 2, 2],
    );
    return {};
  }
  throw new Error("Unknown case " + scenario);
}
run()
  .then(async (result) => {
    console.log(JSON.stringify({ sdkVersion, scenario, ...result }));
    await provider.shutdown();
  })
  .catch(async (error) => {
    console.error(error);
    await provider.shutdown();
    process.exitCode = 1;
  });
