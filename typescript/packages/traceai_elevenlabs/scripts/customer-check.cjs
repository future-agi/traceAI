const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const SDK = require("@elevenlabs/elevenlabs-js");
const { fixture, attributes, AUDIO } = require("./test/fixture.cjs");
async function main() {
  const f = await fixture(SDK);
  try {
    for (const format of ["cjs", "esm"]) {
      const directory = path.join(__dirname, format);
      const before = f.wire.length;
      const child = spawn(
        process.execPath,
        [path.join(directory, "customer.js")],
        {
          cwd: directory,
          env: {
            ...process.env,
            ELEVENLABS_API_KEY: "synthetic-elevenlabs-key-NOT-REAL",
            ELEVENLABS_VOICE_ID: "voice-id",
            ELEVENLABS_BASE_URL: f.baseUrl,
            FI_API_KEY: "synthetic-fi-api",
            FI_SECRET_KEY: "synthetic-fi-secret",
            OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: f.baseUrl + "/v1/traces",
          },
          stdio: "inherit",
        },
      );
      await new Promise((resolve, reject) => {
        child.on("error", reject);
        child.on("exit", (code) =>
          code === 0
            ? resolve()
            : reject(new Error(`Customer ${format}: exit ${code}`)),
        );
      });
      const exports = f.wire.slice(before);
      const spans = exports.flatMap((w) =>
        w.payload.resourceSpans.flatMap((r) => {
          const resource = attributes(r.resource);
          assert.equal(resource.project_name, "speech-app");
          assert.equal(resource.project_type, "observe");
          return r.scopeSpans.flatMap((s) => {
            if (s.scope.name === "@traceai/elevenlabs") {
              const metadata = JSON.parse(fs.readFileSync(path.resolve(path.dirname(require.resolve("@traceai/elevenlabs")), "../../package.json"), "utf8"));
              assert.equal(s.scope.version, metadata.version);
              assert.equal(require("@traceai/elevenlabs").VERSION, metadata.version);
            }
            return s.spans;
          });
        }),
      );
      assert.equal(spans.length, 2);
      const childSpan = spans.find(
        (s) => s.name === "ElevenLabs textToSpeech.convert",
      );
      const parent = spans.find((s) => s.name === "Create greeting");
      assert.equal(childSpan.parentSpanId, parent.spanId);
      assert.equal(childSpan.traceId, parent.traceId);
      assert.equal(
        attributes(childSpan)["gen_ai.request.model"],
        "eleven_multilingual_v2",
      );
      assert.equal(attributes(childSpan)["elevenlabs.completion"], "complete");
      assert.ok(
        exports.every(
          (w) =>
            w.headers["x-api-key"] === "synthetic-fi-api" &&
            w.headers["x-secret-key"] === "synthetic-fi-secret",
        ),
      );
      assert.deepEqual(
        fs.readFileSync(path.join(directory, "speech.mp3")),
        AUDIO,
      );
      assert.ok(
        !JSON.stringify(exports.map((w) => w.payload)).includes(
          "synthetic-fi-secret",
        ),
      );
      console.log(
        `Packed ${format} customer snippet: speech bytes, parent, resource, authentication and OTLP passed`,
      );
    }
  } finally {
    await f.close();
  }
}
void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
