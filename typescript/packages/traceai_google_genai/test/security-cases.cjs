"use strict";
// Real released SDKs and synthetic secrets only; all transport stays mocked.
const assert = require("node:assert/strict");
const path = require("node:path");
const { createRequire } = require("node:module");
const use = createRequire(path.join(process.argv[2], "package.json"));
const scenario = process.argv[3];
const google = use("@google/generative-ai");
const { GoogleGenAIInstrumentation } = use("@traceai/google-genai");
const { NodeTracerProvider } = use("@opentelemetry/sdk-trace-node");
const { InMemorySpanExporter, SimpleSpanProcessor } = use(
  "@opentelemetry/sdk-trace-base",
);
const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
provider.register();
const key = "synthetic-google-secret-PRIVATE";
const prompt = "synthetic-prompt-PRIVATE";
const system = "synthetic-system-PRIVATE";
const image = "SYNTHETIC_IMAGE_PRIVATE";
const output = "synthetic-output-PRIVATE";
const operations = [
  ["generateContent", prompt],
  ["embedContent", prompt],
  [
    "batchEmbedContents",
    { requests: [{ content: { parts: [{ text: prompt }] } }] },
  ],
];
const response = {
  candidates: [
    {
      content: { role: "model", parts: [{ text: output }] },
      finishReason: "STOP",
    },
  ],
};
let inst;
function setup(traceConfig, sdk = google) {
  inst = new GoogleGenAIInstrumentation({
    instrumentationConfig: { enabled: false },
    traceConfig,
  });
  inst.setTracerProvider(provider);
  inst.manuallyInstrument(sdk);
}
function spans() {
  return exporter.getFinishedSpans().map((s) => ({
    name: s.name,
    attributes: s.attributes,
    status: s.status,
    events: s.events,
  }));
}
function assertPrivate(markers) {
  const serialized = JSON.stringify(spans());
  for (const marker of markers)
    assert.ok(!serialized.includes(marker), `telemetry leaked ${marker}`);
}
async function rejected(call) {
  let caught;
  try {
    await call();
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, "original SDK call rejects");
  assert.equal(spans().length, 1);
  assert.equal(spans()[0].status.code, 2);
  assert.equal(spans()[0].events.length, 1);
  assert.equal(spans()[0].events[0].name, "exception");
  return caught;
}
async function run() {
  if (scenario === "credential-errors" || scenario === "credential-header") {
    // Explicit content capture must not enable credential capture.
    const failures = [];
    for (const traceConfig of [
      undefined,
      { hideInputs: false, hideOutputs: false, hideErrorDetails: false },
    ]) {
      setup(traceConfig);
      const credential =
        scenario === "credential-header" ? key + "\ninvalid" : key;
      const model = new google.GoogleGenerativeAI(
        credential,
      ).getGenerativeModel({ model: "synthetic-model" });
      for (const failure of scenario === "credential-header"
        ? ["header"]
        : ["http", "transport"]) {
        global.fetch =
          failure === "http"
            ? async () =>
                new Response(
                  JSON.stringify({
                    error: { message: "synthetic-service-unavailable" },
                  }),
                  {
                    status: 503,
                    statusText: "Service Unavailable",
                    headers: { "content-type": "application/json" },
                  },
                )
            : async () => {
                throw new Error("synthetic-network-failure");
              };
        for (const [method, input] of operations) {
          exporter.reset();
          const caught = await rejected(() => model[method](input));
          // Floor SDK URLs and latest invalid-header errors still reach the app intact.
          if (
            scenario === "credential-header" ||
            use("@google/generative-ai/package.json").version === "0.1.0"
          )
            assert.ok(caught.message.includes(key));
          try {
            assertPrivate([key]);
          } catch {
            failures.push({ failure, method });
          }
        }
      }
      inst.disable();
    }
    assert.deepEqual(failures, [], "SDK failures must not export credentials");
  } else if (scenario === "credential-copies") {
    // Also cover synchronous throws, rejected non-Errors, encoded URLs and
    // sensitive header fields, without mutating any application-owned exception.
    const credential = key + "/+?=";
    const urlKey = "synthetic-url-secret-PRIVATE";
    const headerKey = "synthetic-header-secret-PRIVATE";
    const message =
      `safe-diagnostic ${credential} ${encodeURIComponent(credential)} ` +
      `https://example.invalid/path?key=${urlKey}&safe=1\n` +
      `X-Goog-Api-Key: ${headerKey}\nAuthorization: Bearer ${headerKey}\n` +
      `headers={"x-api-key":"${headerKey}"}`;
    let thrown;
    let sync;
    class Model {
      constructor() {
        this.apiKey = credential;
        this.model = "synthetic-model";
      }
      generateContent() {
        if (sync) throw thrown;
        return Promise.reject(thrown);
      }
    }
    setup({}, { GenerativeModel: Model });
    const model = new Model();
    for (sync of [true, false]) {
      for (const value of [new Error(message), message]) {
        thrown = value;
        const original =
          typeof value === "string"
            ? value
            : { name: value.name, message: value.message, stack: value.stack };
        exporter.reset();
        assert.equal(
          await rejected(() => model.generateContent("safe-input")),
          value,
        );
        assertPrivate([key, urlKey, headerKey, encodeURIComponent(credential)]);
        assert.ok(spans()[0].status.message.includes("safe-diagnostic"));
        if (typeof value !== "string")
          assert.deepEqual(
            { name: value.name, message: value.message, stack: value.stack },
            original,
          );
      }
    }
    // Redaction uses the request's key, not a later mutation of the model.
    exporter.reset();
    thrown = new Error(credential);
    sync = false;
    const pending = model.generateContent("safe-input");
    model.apiKey = "synthetic-replacement-key";
    assert.equal(await rejected(() => pending), thrown);
    assertPrivate([key]);

    // Even uninspectable application errors retain their identity and ERROR span.
    exporter.reset();
    thrown = {
      toString() {
        throw new Error(key);
      },
    };
    assert.equal(
      await rejected(() => model.generateContent("safe-input")),
      thrown,
    );
    assertPrivate([key]);
  } else if (scenario === "privacy-combinations") {
    const flags = [
      "hideInputs",
      "hideInputText",
      "hideInputMessages",
      "hideInputImages",
      "hideOutputs",
      "hideOutputText",
      "hideOutputMessages",
      "hideEmbeddingVectors",
    ];
    const input = {
      contents: [
        {
          role: "user",
          parts: [
            { text: prompt },
            { inlineData: { mimeType: "image/png", data: image } },
          ],
        },
      ],
    };
    const model = new google.GoogleGenerativeAI(key).getGenerativeModel({
      model: "synthetic-model",
      systemInstruction: system,
    });
    const failures = [];
    for (let bits = 0; bits < 1 << flags.length; bits++) {
      const config = Object.fromEntries(
        flags.map((f, i) => [f, !!(bits & (1 << i))]),
      );
      exporter.reset();
      setup(config);
      global.fetch = async (url, options) => {
        const body = JSON.parse(options.body);
        const payload = String(url).includes(":batchEmbedContents")
          ? { embeddings: body.requests.map(() => ({ values: [0.123456789] })) }
          : String(url).includes(":embedContent")
            ? { embedding: { values: [0.123456789] } }
            : response;
        // Masking must not change the real SDK request body.
        assert.ok(options.body.includes(prompt));
        if (String(url).includes(":generateContent"))
          assert.ok(options.body.includes(image));
        return new Response(JSON.stringify(payload), {
          headers: { "content-type": "application/json" },
        });
      };
      assert.equal(
        (await model.generateContent(input)).response.text(),
        output,
      );
      await model.embedContent(prompt);
      await model.batchEmbedContents(operations[2][1]);
      assert.equal(spans().length, 3);
      const hidden = [];
      if (config.hideInputs || config.hideInputText || config.hideInputMessages)
        hidden.push(prompt, system);
      if (config.hideInputs || config.hideInputImages) hidden.push(image);
      if (
        config.hideOutputs ||
        config.hideOutputText ||
        config.hideOutputMessages
      )
        hidden.push(output);
      if (config.hideOutputs || config.hideEmbeddingVectors)
        hidden.push("0.123456789");
      try {
        assertPrivate(hidden);
      } catch {
        failures.push(config);
      }
      if (
        !config.hideInputs &&
        !config.hideInputText &&
        !config.hideInputMessages
      )
        assert.ok(
          JSON.stringify(spans()).includes(prompt),
          "unhidden text remains available",
        );
      inst.disable();
    }
    assert.deepEqual(failures, [], "all 256 privacy combinations must compose");
  } else throw new Error("Unknown scenario");
}
run()
  .then(() => console.log(JSON.stringify({ scenario, passed: true })))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    inst?.disable();
    await provider.shutdown();
  });
