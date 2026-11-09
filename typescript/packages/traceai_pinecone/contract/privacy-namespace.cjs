// Run with node --test after building. PINECONE_SDK_DIR may select an
// independently installed SDK. All provider and OTLP traffic stays on loopback.
const assert = require("node:assert/strict");
const http = require("node:http");
const { once } = require("node:events");
const { createRequire } = require("node:module");
const { resolve } = require("node:path");
const { spawnSync } = require("node:child_process");

const marker = "synthetic-query-filter-value";
const filter = { category: { $eq: marker } };

async function child(mode) {
  const { register, ProjectType } = require("@traceai/fi-core");
  const { PineconeInstrumentation } = require("../dist/src");
  const requests = [];
  const exports = [];
  const receiver = http.createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", chunk => { body += chunk; });
    request.on("end", () => {
      exports.push({ path: request.url, body: JSON.parse(body) });
      response.writeHead(200, { "content-type": "application/json" });
      response.end("{}");
    });
  });
  const service = http.createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", chunk => { body += chunk; });
    request.on("end", () => {
      requests.push({ method: request.method, path: request.url, body: body ? JSON.parse(body) : null });
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ matches: [{ id: "synthetic-record", score: 0.9 }], vectors: [] }));
    });
  });
  receiver.listen(0, "127.0.0.1");
  service.listen(0, "127.0.0.1");
  await Promise.all([once(receiver, "listening"), once(service, "listening")]);
  const provider = register({
    projectName: "pinecone-privacy-contract",
    projectType: ProjectType.OBSERVE,
    endpoint: `http://127.0.0.1:${receiver.address().port}`,
    headers: {},
  });
  const instrumentation = new PineconeInstrumentation();
  try {
    const load = process.env.PINECONE_SDK_DIR
      ? createRequire(resolve(process.env.PINECONE_SDK_DIR, "package.json")) : require;
    const sdk = load("@pinecone-database/pinecone");
    const version = load("@pinecone-database/pinecone/package.json").version;
    const major = Number(version.split(".")[0]);
    const config = {
      apiKey: "synthetic-test-key",
      // Use the SDK's own validation, serialization and response parsing. Only
      // redirect its transport to the local HTTP service, which records requests.
      fetchApi: (url, options) => {
        const target = new URL(String(url));
        assert.equal(target.hostname, "pinecone.invalid");
        return fetch(`http://127.0.0.1:${service.address().port}${target.pathname}${target.search}`, options);
      },
    };
    const index = major >= 7
      ? new sdk.Index({ name: "synthetic-index", namespace: "scoped-namespace", host: "https://pinecone.invalid" }, config)
      : new sdk.Index("synthetic-index", config, "scoped-namespace", "https://pinecone.invalid");
    const spans = () => exports.flatMap(entry => {
      assert.equal(entry.path, "/tracer/v1/traces");
      return entry.body.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans));
    });
    const attributes = span => Object.fromEntries(span.attributes.map(({ key, value }) => [key, value.stringValue ?? value.intValue]));
    if (mode === "privacy") {
      const result = await index.query({ vector: [0.1, 0.2], topK: 1, filter });
      await provider.forceFlush();
      console.log(JSON.stringify({ version, requests, exports }));
      assert.equal(result.matches[0].id, "synthetic-record");
      assert.equal(requests.length, 1);
      assert.deepEqual(requests[0].body.filter, filter, "hiding must not alter the provider request");
      assert.equal(spans().length, 1);
      const attrs = attributes(spans()[0]);
      assert.equal(attrs["db.vector.index.name"], "synthetic-index");
      assert.equal(attrs["db.vector.namespace"], "scoped-namespace");
      assert.equal(attrs["db.vector.results.count"], 1);
      const hidden = process.env.FI_HIDE_INPUTS === "true" || process.env.FI_HIDE_INPUT_TEXT === "true";
      assert.equal(attrs["db.vector.query.filter"], hidden ? "__REDACTED__" : JSON.stringify(filter));
      // Scan every received field, including events, links and resources, not
      // just the query-filter attribute inspected above.
      assert.equal(JSON.stringify(exports).includes(marker), !hidden);
    } else {
      // Per-call namespace overrides are part of the SDK 7+ object-argument API.
      // Older SDKs are still checked for their normal scoped namespace.
      const namespaces = major >= 7 ? [undefined, "", "override-namespace"] : [undefined];
      const methods = ["query", "update"];
      if (typeof index.listPaginated === "function") methods.push("listPaginated");
      for (const method of methods) {
        for (const namespace of namespaces) {
          requests.length = 0;
          exports.length = 0;
          const options = method === "query" ? { vector: [0.1, 0.2], topK: 1 }
            : method === "update" ? { id: "synthetic-record", metadata: { category: "synthetic" } } : { limit: 1 };
          if (namespace !== undefined) options.namespace = namespace;
          await index[method](options);
          await provider.forceFlush();
          console.log(JSON.stringify({ version, method, namespace, requests, exports }));
          assert.equal(requests.length, 1);
          const sentNamespace = method === "listPaginated"
            ? new URL(requests[0].path, "http://127.0.0.1").searchParams.get("namespace")
            : requests[0].body.namespace;
          const expected = namespace ?? "scoped-namespace";
          assert.equal(sentNamespace, expected, `${method}: request namespace`);
          assert.equal(spans().length, 1);
          const attrs = attributes(spans()[0]);
          assert.equal(attrs["db.operation.name"], method === "listPaginated" ? "list" : method);
          assert.equal(attrs["db.vector.namespace"], expected || undefined, `${method}: vector namespace`);
          assert.equal(attrs["db.namespace"], expected || undefined, `${method}: database namespace`);
        }
      }
    }
  } finally {
    instrumentation.disable();
    await provider.shutdown();
    await Promise.all([receiver, service].map(server => new Promise((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      server.closeAllConnections();
    })));
  }
}

if (process.argv[2] === "--child") {
  child(process.argv[3]).catch(error => { console.error(error); process.exitCode = 1; });
} else {
  const { test } = require("node:test");
  const run = (mode, flags) => {
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      if (key.startsWith("FI_") || key.startsWith("OTEL_") || key === "NODE_TEST_CONTEXT") delete env[key];
    }
    Object.assign(env, flags);
    const result = spawnSync(process.execPath, [__filename, "--child", mode], { env, encoding: "utf8", timeout: 30000 });
    process.stdout.write(result.stdout || "");
    process.stderr.write(result.stderr || "");
    assert.equal(result.error, undefined);
    assert.equal(result.status, 0, `${mode} contract failed with ${JSON.stringify(flags)}`);
  };
  for (const flag of ["FI_HIDE_INPUTS", "FI_HIDE_INPUT_TEXT"]) {
    for (const value of ["false", "true"]) {
      test(`${flag}=${value}: query filter and complete OTLP payload`, () => run("privacy", { [flag]: value }));
    }
  }
  test("scoped, default and explicit namespace attribution", () => run("namespace", {}));
}
