import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { gunzipSync, inflateSync } from "node:zlib";

// Prerequisites: build @traceai/auto and its workspace dependencies, and pip install
// the local python/ wheel into TRACEAI_TEST_PYTHON's interpreter (default python3).
// Deliberately no prerequisite skip: missing zero-code bootstrap must fail loudly.
const python = process.env.TRACEAI_TEST_PYTHON || "python3";
const root = resolve(__dirname, ".."), apps = resolve(__dirname, "apps/ratio-cross-language");
const keys = ["ratio-fi-api-PLACEHOLDER", "ratio-fi-secret-PLACEHOLDER"];
const ratioKey = "fi.sampling.ratio";
// Python TraceIdRatioBased compares the low 64 bits to 2**63 at rate 0.5.
// Change ONLY the root id generator: production sampling decisions remain intact.
const cases = [
  { label: "kept", traceId: "00000000000000000000000000000001", sampled: true },
  { label: "dropped", traceId: "ffffffffffffffffffffffffffffffff", sampled: false },
];

function environment(cwd: string): NodeJS.ProcessEnv {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (/^(FI_|OTEL_|TRACEAI_|TEST_|PYTHON)/.test(key) || /^.*_proxy$/i.test(key)
      || ["NODE_OPTIONS", "NODE_PATH"].includes(key)) delete env[key];
  }
  return { ...env, HOME: cwd, XDG_CONFIG_HOME: cwd, FI_CONFIG_FILE: join(cwd, "futureagi.toml"),
    NO_PROXY: "*", no_proxy: "*", PYTHONNOUSERSITE: "1", PYTHONUNBUFFERED: "1" };
}

interface Child {
  proc: ChildProcessWithoutNullStreams;
  stdout: string;
  stderr: string;
  done: boolean;
  closed: Promise<void>;
  check(): void;
}
function launch(command: string, args: string[], cwd: string, env: NodeJS.ProcessEnv): Child {
  const proc = spawn(command, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
  let failure: Error | undefined;
  const child: Child = { proc, stdout: "", stderr: "", done: false, closed: Promise.resolve(),
    check() { if (failure) throw new Error(`${failure.message}\nstdout: ${child.stdout}\nstderr: ${child.stderr}`); } };
  // Async children are essential: spawnSync would deadlock their exporters against
  // the collector running on this Jest process's event loop.
  const timer = setTimeout(() => { failure = new Error(`${command} timed out`); proc.kill("SIGKILL"); }, 35000);
  proc.stdout.on("data", chunk => { child.stdout += chunk; });
  proc.stderr.on("data", chunk => { child.stderr += chunk; });
  // A decoder can exit before consuming stdin if dependencies are missing.
  proc.stdin.on("error", error => { failure ??= error; });
  proc.on("error", error => { failure = error; });
  child.closed = new Promise<void>(resolveClosed => proc.once("close", (code, signal) => {
    clearTimeout(timer); child.done = true;
    if (code !== 0) failure ??= new Error(`${command} exited ${code} (${signal})`);
    resolveClosed();
  }));
  return child;
}
async function ready(child: Child): Promise<string> {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const line = child.stdout.split("\n").find(line => line.startsWith('{"ready":'));
    if (line && child.stdout.includes(line + "\n")) return JSON.parse(line).ready;
    child.check();
    if (child.done) throw new Error(`Node exited before listening: ${child.stderr}`);
    await new Promise(resolveWait => setTimeout(resolveWait, 20));
  }
  throw new Error(`Node did not become ready: ${child.stdout}\n${child.stderr}`);
}
async function stop(child: Child): Promise<void> {
  if (!child.done) child.proc.kill("SIGKILL");
  await child.closed;
}
function attributes(span: any): Record<string, unknown> {
  return Object.fromEntries((span.attributes ?? []).map((a: any) => [a.key,
    a.value.stringValue ?? a.value.doubleValue ?? a.value.intValue ?? a.value.boolValue]));
}
function spans(envelopes: any[]): any[] {
  return envelopes.flatMap(e => e.resourceSpans ?? []).flatMap(resource =>
    (resource.scopeSpans ?? []).flatMap((scope: any) => (scope.spans ?? []).map((span: any) =>
      ({ ...span, project: attributes(resource.resource).project_name }))));
}

test.each(cases)("Python zero-code -> Node default sampler: $label trace", async ({ traceId, sampled }) => {
  const cwd = mkdtempSync(join(tmpdir(), "traceai-ratio-cross-language-"));
  const children: Child[] = [];
  const collected: { type: string; body: Buffer }[] = [], errors: string[] = [];
  let modelCalls = 0;
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("error", error => { errors.push(error.message); res.destroy(); });
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      try {
        let body: Buffer = Buffer.concat(chunks);
        if (req.headers["content-encoding"] === "gzip") body = gunzipSync(body);
        else if (req.headers["content-encoding"] === "deflate") body = inflateSync(body);
        if (req.method === "POST" && req.url === "/tracer/v1/traces") {
          const type = String(req.headers["content-type"] ?? "");
          if (!/application\/(json|x-protobuf)/.test(type)) throw new Error(`Unexpected OTLP content type ${type}`);
          collected.push({ type, body });
          res.writeHead(200, { "content-type": type });
          res.end(type.includes("json") ? "{}" : Buffer.alloc(0));
        } else if (req.method === "POST" && req.url === "/v1/chat/completions") {
          modelCalls++;
          JSON.parse(body.toString());
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ id: "chat-ratio-fixture", model: "gpt-fixture",
            choices: [{ index: 0, message: { role: "assistant", content: "local answer" }, finish_reason: "stop" }],
            usage: { prompt_tokens: 2, completion_tokens: 2, total_tokens: 4 } }));
        } else throw new Error(`Unexpected local request ${req.method} ${req.url}`);
      } catch (error) {
        errors.push(String(error)); res.writeHead(500).end();
      }
    });
  });
  const start = (command: string, args: string[], env: NodeJS.ProcessEnv) => {
    const child = launch(command, args, cwd, env); children.push(child); return child;
  };
  try {
    // An explicit empty file and isolated HOME/cwd prevent ambient rate/credentials
    // from a discovered config file from changing either process's configuration.
    writeFileSync(join(cwd, "futureagi.toml"), "");
    // Optional instrumentors resolve from the host application's cwd, not the
    // preload package. Give this isolated app the installed fixture dependencies.
    symlinkSync(join(root, "node_modules"), join(cwd, "node_modules"), "dir");
    await new Promise<void>((resolveListen, reject) => {
      server.once("error", reject); server.listen(0, "127.0.0.1", resolveListen);
    });
    const collector = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    const base = { ...environment(cwd), FI_API_KEY: keys[0], FI_SECRET_KEY: keys[1], FI_BASE_URL: collector };
    const node = start(process.execPath, ["-r", resolve(root, "dist/src/register.js"), join(apps, "node.cjs")],
      { ...base, FI_PROJECT_NAME: "ratio-node", FI_INSTRUMENTATIONS: "wire,http", TEST_COLLECTOR: collector });
    const url = await ready(node);
    const py = start(python, [join(apps, "python.py"), url],
      { ...base, FI_AUTO_INSTRUMENT: "true", FI_PROJECT_NAME: "ratio-python",
        FI_INSTRUMENTATIONS: "wire", FI_SAMPLE_RATE: "0.5", TEST_TRACE_ID: traceId });
    py.proc.stdin.end();
    await py.closed; py.check();
    node.proc.stdin.end();
    await node.closed; node.check();
    // Both real exporters have flushed and shut down before checking absence.
    // No sleep-based "nothing arrived yet" assertion in the dropped case.
    expect(errors).toEqual([]);
    expect(modelCalls).toBe(1);
    const result = JSON.parse(py.stdout.trim());
    expect(result).toMatchObject({ traceId, sampled, recording: sampled });
    expect(result.spanId).toMatch(/^[0-9a-f]{16}$/);
    expect(result.spanId).not.toBe("0000000000000000");
    const traceparent = `00-${traceId}-${result.spanId}-${sampled ? "01" : "00"}`;
    expect(result.sent.traceparent).toBe(traceparent);
    expect(result.received.traceparent).toBe(traceparent);
    if (sampled) {
      expect(result.sent.tracestate.split(",")).toContain("fi=r:0.5");
      expect(result.received.tracestate.split(",")).toContain("fi=r:0.5");
    }

    const json = collected.filter(item => item.type.includes("json")).map(item => JSON.parse(item.body.toString()));
    const protobuf = collected.filter(item => item.type.includes("x-protobuf"));
    let decoded: any[] = [];
    if (protobuf.length) {
      const decoder = start(python, [join(apps, "python.py"), "--decode"], environment(cwd));
      decoder.proc.stdin.end(JSON.stringify(protobuf.map(item => item.body.toString("base64"))));
      await decoder.closed; decoder.check();
      decoded = JSON.parse(decoder.stdout.trim());
    }
    const exported = spans([...json, ...decoded]);
    for (const key of keys) expect(JSON.stringify(exported)).not.toContain(key);
    if (!sampled) {
      expect(exported).toEqual([]);
      expect(collected).toHaveLength(0);
      return;
    }
    expect(json.length).toBeGreaterThan(0);
    expect(protobuf.length).toBeGreaterThan(0);
    const pythonSpans = exported.filter(span => span.project === "ratio-python");
    const nodeSpans = exported.filter(span => span.project === "ratio-node");
    expect(pythonSpans).toHaveLength(1);
    expect(nodeSpans).toHaveLength(2); // incoming SERVER plus its real wire AI child
    expect(exported).toHaveLength(3);
    expect(pythonSpans[0]).toMatchObject({ name: "python-root", spanId: result.spanId, traceId });
    expect([undefined, ""]).toContain(pythonSpans[0].parentSpanId);
    const incoming = nodeSpans.find(span => span.kind === 2);
    const ai = nodeSpans.find(span => span.kind === 3);
    expect(incoming).toMatchObject({ traceId, parentSpanId: result.spanId });
    expect(ai).toMatchObject({ traceId, parentSpanId: incoming.spanId });
    expect(incoming.traceState.split(",")).toContain("fi=r:0.5");
    // Contract, intentionally stronger than the current local sampler tests:
    // Node has NO local ratio configured but must inherit the Python root's rate.
    // Never "fix" this by setting a fractional Node sampler or stamping fixtures.
    expect(exported.map(span => ({ project: span.project, name: span.name,
      traceId: span.traceId, ratio: attributes(span)[ratioKey] }))).toEqual(
      exported.map(span => ({ project: span.project, name: span.name, traceId, ratio: 0.5 })));
  } finally {
    await Promise.all(children.map(stop));
    server.closeAllConnections();
    await new Promise<void>(resolveClose => server.close(() => resolveClose()));
    rmSync(cwd, { recursive: true, force: true });
  }
}, 90000);
