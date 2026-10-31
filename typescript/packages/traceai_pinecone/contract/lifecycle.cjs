// Run after building the package. Optional argument: a directory containing an
// independently installed Pinecone SDK, for example /tmp/pinecone-sdk-2.
const assert = require("node:assert/strict");
const http = require("node:http");
const { once } = require("node:events");
const { createRequire } = require("node:module");
const { resolve } = require("node:path");
const { register, ProjectType } = require("@traceai/fi-core");
const { PineconeInstrumentation, isPatched } = require("../dist/src");

async function main() {
  const requests = [];
  const receiver = http.createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", chunk => { body += chunk; });
    request.on("end", () => {
      requests.push({ path: request.url, body: JSON.parse(body) });
      response.writeHead(200, { "content-type": "application/json" });
      response.end("{}");
    });
  });
  receiver.listen(0, "127.0.0.1");
  await once(receiver, "listening");
  const provider = register({
    projectName: "pinecone-lifecycle-test",
    projectType: ProjectType.OBSERVE,
    endpoint: `http://127.0.0.1:${receiver.address().port}`,
    headers: {},
  });
  const instrumentation = new PineconeInstrumentation();
  try {
    // Register the actual require hook before loading the real SDK.
    const load = process.argv[2]
      ? createRequire(resolve(process.argv[2], "package.json"))
      : require;
    const sdk = load("@pinecone-database/pinecone");
    const version = load("@pinecone-database/pinecone/package.json").version;
    if (process.argv[3] === "unsupported") {
      assert.equal(isPatched(), false, `SDK ${version} must not be automatically patched`);
      console.log(JSON.stringify({ version, automaticallyPatched: false }));
      return;
    }
    assert.equal(isPatched(), true, `SDK ${version} must be automatically patched`);
    let calls = 0;
    const index = new sdk.Index("test-index", {
      apiKey: "offline-test-key",
      fetchApi: async () => {
        calls++;
        return new Response(JSON.stringify({ matches: [{ id: "record", score: 0.9 }] }), {
          headers: { "content-type": "application/json" },
        });
      },
    }, "test-namespace", "https://pinecone.invalid");
    const counts = [];
    for (let phase = 0; phase < 3; phase++) {
      requests.length = 0;
      const result = await index.query({ vector: [0.1, 0.2], topK: 1 });
      assert.equal(result.matches[0].id, "record");
      await provider.forceFlush();
      const spans = requests.flatMap(request => {
        assert.equal(request.path, "/tracer/v1/traces");
        return request.body.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans));
      });
      counts.push(spans.length);
      for (const span of spans) {
        assert.equal(span.name, "pinecone query");
        assert.equal(span.kind, 3); // OTLP's enum value for CLIENT.
        const attributes = Object.fromEntries(span.attributes.map(attribute => [attribute.key, attribute.value]));
        assert.equal(attributes["db.system"].stringValue, "pinecone");
        assert.equal(attributes["db.vector.index.name"].stringValue, "test-index");
        assert.equal(attributes["db.vector.namespace"].stringValue, "test-namespace");
        assert.equal(attributes["db.namespace"].stringValue, "test-namespace");
        assert.equal(attributes["db.vector.results.count"].intValue, 1);
      }
      if (phase === 0) instrumentation.disable();
      if (phase === 1) instrumentation.enable();
    }
    assert.deepEqual(counts, [1, 0, 1]);
    assert.equal(calls, 3);
    console.log(JSON.stringify({ version, automaticLifecycleWireSpans: counts, sdkCalls: calls }));
  } finally {
    instrumentation.disable();
    await provider.shutdown();
    receiver.close();
    await once(receiver, "close");
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
