const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { spawn } = require("node:child_process");
const core = require("@traceai/agent-core");
async function fixture(t, capture = true) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-cursor-test-"));
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      requests.push({
        headers: req.headers,
        body: JSON.parse(body),
        url: req.url,
      });
      res.setHeader("Content-Type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const options = { agent: "cursor", home, env: {} };
  await core.setup({
    ...options,
    credentials: {
      apiKey: "synthetic-cursor-api",
      secretKey: "synthetic-cursor-secret",
    },
    projectName: "cursor-test",
    otlpEndpoint: endpoint,
    captureContent: capture,
    configureOnly: true,
  });
  const config = await core.resolveConfig(options);
  const context = { config, scope: "user" };
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    // A detached late-transcript finisher may still be writing state as the test ends.
    await fs.rm(home, { force: true, recursive: true, maxRetries: 20, retryDelay: 100 });
  });
  return { home, options, config, context, requests, endpoint };
}
function payload(name, extra = {}) {
  return {
    cursor_version: "3.24.18",
    conversation_id: "session-1",
    generation_id: "turn-1",
    hook_event_name: name,
    ...extra,
  };
}
function attributes(span) {
  return Object.fromEntries(
    span.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
}
function spans(requests) {
  return requests.flatMap((r) =>
    (r.body.resourceSpans ?? []).flatMap((r) =>
      r.scopeSpans.flatMap((s) => s.spans),
    ),
  );
}
function child(command, input, env) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const p = spawn("/bin/sh", ["-c", command], { env });
    let stdout = "",
      stderr = "";
    p.stdout.on("data", (c) => (stdout += c));
    p.stderr.on("data", (c) => (stderr += c));
    p.on("error", reject);
    p.stdin.on("error", (error) => {
      if (error.code !== "EPIPE") reject(error);
    });
    p.on("exit", (code) =>
      resolve({ code, stdout, stderr, ms: Date.now() - started }),
    );
    p.stdin.end(typeof input === "string" ? input : JSON.stringify(input));
  });
}
module.exports = { fixture, payload, attributes, spans, child, core, fs, path };
