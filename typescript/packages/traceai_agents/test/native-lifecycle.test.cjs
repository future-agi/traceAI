const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { Runtime, runtimeRequest } = require("../dist/src/runtime/daemon.js");
const { AdapterRegistry } = require("../dist/src/adapters.js");
const { saveConfig } = require("../dist/src/config.js");
const {
  FileSecretStore,
  newCredentials,
} = require("../dist/src/security/credentials.js");
const { copilotCliAdapter } = require("../dist/src/profiles/copilot.js");
const source = require("../../../../integrations/coding-agents/fixtures/source/copilot-cli-1.0.98.json");

async function fixture(t, held = false) {
  // Short real paths also qualify the actual Unix socket on Darwin; no preload.
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "fi-native-")),
  );
  const p = {
    config: path.join(root, "config"),
    state: path.join(root, "state"),
    credentials: path.join(root, "credentials"),
    socket: path.join(root, "runtime.sock"),
    configFile: path.join(root, "config/config.json"),
  };
  const binding = {
    id: "00000000-0000-4000-8000-000000000001",
    project: "fixture",
    endpoint: "http://127.0.0.1:1/v1/traces",
    approvedOrigin: "http://127.0.0.1:1",
    loopbackTest: true,
    // No fabricated backend contract: local capture must remain export-held.
  };
  const route = {
    id: "copilot.cli",
    binding,
    enabled: true,
    epoch: 1,
    policy: { revision: "1", categories: [], ceiling: [] },
  };
  const config = {
    schema_version: 1,
    profiles: {
      fixture: { binding, storage: "file", credentialRef: "fixture" },
    },
    routes: {
      "copilot.cli": {
        route,
        agent: "copilot",
        surface: "cli",
        profile: "fixture",
        transcriptConsent: false,
        activation: {
          installed: true,
          trusted: "unknown",
          activated: false,
          restartRequired: false,
        },
      },
    },
  };
  saveConfig(p, config);
  await new FileSecretStore(p.credentials).set(
    "fixture",
    newCredentials({
      api_key: "synthetic-fi-api",
      secret_key: "synthetic-fi-secret",
    }),
  );
  // Test registry qualifies only the transport lifecycle, NOT real Copilot policy/activation.
  const adapter = held
    ? copilotCliAdapter
    : { ...copilotCliAdapter, activationHolds: [] };
  const runtime = new Runtime(
    p,
    path.resolve(__dirname, "../dist/src/runtime/storage-child.js"),
    "0.1.0",
    new AdapterRegistry([adapter]),
  );
  t.after(async () => {
    await runtime.stop();
    fs.rmSync(root, { recursive: true, force: true });
  });
  await runtime.start();
  const rpc = (message) => runtimeRequest(p.socket, message);
  const open = () => rpc({ operation: "native-open", route: "copilot.cli" });
  return { root, p, config, runtime, rpc, open };
}
async function send(lease, headers = {}, value = source) {
  const response = await fetch(lease.endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + lease.token,
      ...headers,
    },
    body: JSON.stringify(value),
  });
  await response.arrayBuffer();
  return response.status;
}

test("runtime-owned native lease captures durably, authenticates locally, and records real activation without backend approval", async (t) => {
  const f = await fixture(t);
  const lease = await f.open();
  assert.match(lease.endpoint, /^http:\/\/127\.0\.0\.1:\d+\/v1\/traces$/);
  assert.match(lease.token, /^[a-f0-9]{64}$/);
  assert.equal(
    await send(lease, { Authorization: "Bearer synthetic-fi-api" }),
    403,
  );
  assert.equal(await send(lease, { Origin: "https://untrusted.invalid" }), 403);
  assert.equal(await send(lease), 200);
  assert.equal(
    await send(lease),
    200,
    "same native IDs replay locally, not new work",
  );
  const status = await f.rpc({ operation: "status" });
  assert.equal(status.agents[0].activation.activated, true);
  assert.equal(status.agents[0].native.active_sessions, 1);
  assert.equal(typeof status.agents[0].native.last_durable_ack, "number");
  assert.equal(status.agents[0].export, "HELD_BACKEND_CONTRACT");
  assert.equal(status.persistence, "unknown");
  assert.ok(!JSON.stringify(status).includes(lease.token));
  for (const dir of [f.p.config, path.join(f.p.state, "spool")])
    for (const file of fs.readdirSync(dir)) {
      const filename = path.join(dir, file);
      if (fs.statSync(filename).isFile()) {
        const bytes = fs.readFileSync(filename);
        assert.ok(!bytes.includes(Buffer.from(lease.token)));
        assert.ok(!bytes.includes(Buffer.from("synthetic-fi-api")));
      }
    }
  await f.rpc({ operation: "native-close", id: lease.id });
  await assert.rejects(send(lease));
  await f.rpc({ operation: "native-close", id: lease.id }); // idempotent cleanup
});

test("an upstream authentication hold still captures locally with the retained identity key", async (t) => {
  const f = await fixture(t);
  const { removeAuth } = require("../dist/src/security/credentials.js");
  await removeAuth(new FileSecretStore(f.p.credentials), "fixture");
  await f
    .rpc({ operation: "auth-rotated", profile: "fixture" })
    .catch(() => {});
  const lease = await f.open();
  assert.equal(await send(lease), 200);
  const status = await f.rpc({ operation: "status" });
  assert.equal(status.agents[0].activation.activated, true);
  assert.equal(status.agents[0].export, "HELD_BACKEND_CONTRACT");
});

test("native admission cannot bypass adapter qualification or disabled routes", async (t) => {
  const f = await fixture(t, true);
  await assert.rejects(f.open(), /COPILOT_SIGNAL_ISOLATION_UNQUALIFIED/);
  await f.rpc({ operation: "disable", route: "copilot.cli" });
  await assert.rejects(f.open(), /HELD_DISABLED/);
});

test("disable revokes every launch and re-enable cannot revive old capabilities", async (t) => {
  const f = await fixture(t),
    one = await f.open(),
    two = await f.open();
  await f.rpc({ operation: "disable", route: "copilot.cli" });
  for (const lease of [one, two]) {
    await assert.rejects(send(lease));
    await assert.rejects(
      f.rpc({ operation: "native-renew", id: lease.id }),
      /HELD_NATIVE_SESSION/,
    );
  }
  f.config.routes["copilot.cli"].route.epoch = 3;
  saveConfig(f.p, f.config);
  await f.rpc({ operation: "refresh" });
  const next = await f.open();
  assert.notEqual(next.token, one.token);
  assert.equal(await send(next, { Authorization: "Bearer " + one.token }), 403);
  assert.equal(await send(next), 200);
});

async function launch(f, mode) {
  const driver = path.join(f.root, "driver.cjs");
  const agent = path.join(f.root, "agent.cjs");
  fs.writeFileSync(
    agent,
    `
    const assert = require('node:assert/strict');
    (async () => {
      assert.equal(process.env.FI_API_KEY, undefined);
      assert.equal(process.env.FI_SECRET_KEY, undefined);
      assert.equal(process.env.FI_AGENT_STATE_HOME, undefined);
      assert.equal(process.env.OPENAI_API_KEY, 'synthetic-provider');
      assert.equal(process.env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT, 'http://127.0.0.1:1/v1/metrics');
      if (process.argv[2] === 'unavailable') {
        assert.equal(process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, undefined);
        assert.equal(process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS, undefined);
      } else {
        assert.equal(process.env.OTEL_EXPORTER_OTLP_TRACES_PROTOCOL, 'http/json');
        const response = await fetch(process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, {
          method: 'POST', headers: { 'Content-Type': 'application/json',
            Authorization: decodeURIComponent(process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS.split('=')[1]) },
          body: JSON.stringify(${JSON.stringify(source)})
        });
        assert.equal(response.status, 200);
        await response.arrayBuffer();
      }
      if (process.argv[2] === 'forwarded') {
        process.stdout.write('ready\\n');
        setInterval(() => {}, 1000);
        return;
      }
      if (process.argv[2] === 'signal') process.kill(process.pid, 'SIGTERM');
      else process.exit(process.argv[2] === 'failure' ? 23 : 0);
    })().catch(() => process.exit(99));
  `,
  );
  const runModule = path.resolve(__dirname, "../dist/src/cli/run.js");
  fs.writeFileSync(
    driver,
    `
    const {runAgent} = require(${JSON.stringify(runModule)});
    const adapter = { launch: async () => ({ executable: process.execPath, args: [${JSON.stringify(agent)}], env: {}, native: true }) };
    runAgent(adapter, ${JSON.stringify({ routeRef: "copilot.cli", socketPath: f.p.socket })}, [${JSON.stringify(mode)}])
      .then(result => { if (result.signal) process.kill(process.pid, result.signal); else process.exitCode = result.code; })
      .catch(() => process.exitCode = 98);
  `,
  );
  const child = spawn(process.execPath, [driver], {
    env: {
      HOME: f.root,
      PATH: process.env.PATH,
      FI_API_KEY: "synthetic-fi-api",
      FI_SECRET_KEY: "synthetic-fi-secret",
      FI_AGENT_STATE_HOME: f.p.state,
      OPENAI_API_KEY: "synthetic-provider",
      OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: "http://127.0.0.1:1/v1/metrics",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (b) => {
    output += b;
    if (mode === "forwarded" && output.includes("ready")) child.kill("SIGTERM");
  });
  child.stderr.on("data", (b) => {
    output += b;
  });
  const timer = setTimeout(() => child.kill("SIGKILL"), 10_000);
  return new Promise((resolve) =>
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, output });
    }),
  );
}

test("managed run owns native ingress, preserves agent failures/signals, and never passes FI credentials", async (t) => {
  const f = await fixture(t);
  for (const mode of ["success", "failure", "signal", "forwarded"]) {
    const result = await launch(f, mode);
    assert.deepEqual(
      [result.code, result.signal],
      mode === "signal" || mode === "forwarded"
        ? [null, "SIGTERM"]
        : [mode === "failure" ? 23 : 0, null],
      result.output,
    );
    const status = await f.rpc({ operation: "status" });
    assert.equal(status.agents[0].native.active_sessions, 0);
    assert.equal(status.agents[0].activation.activated, true);
  }
});
test("unavailable runtime never changes the coding result or adds a dead trace destination", async (t) => {
  const f = await fixture(t);
  await f.runtime.stop();
  const result = await launch(f, "unavailable");
  assert.equal(result.code, 0, result.output);
  assert.match(result.output, /tracing unavailable/);
  assert.ok(!result.output.includes("synthetic-fi"));
});

test("abandoned native leases expire, renewals are bounded, and revocation fences in-flight opens", async () => {
  const { NativeSessions } = require("../dist/src/runtime/native.js");
  const key = Buffer.alloc(32, 9);
  const source = {
    adapter: copilotCliAdapter,
    context: {
      identityKey: key,
      policyRevision: "1",
      sourceSchema: copilotCliAdapter.capability.schemas[0],
    },
    check() {},
    admit: async () => {},
  };
  // Wide real-timer margins for loaded CI runners; renewal remains required.
  const manager = new NativeSessions(async () => source, 1000);
  const lease = await manager.open("copilot.cli");
  await new Promise((r) => setTimeout(r, 300));
  manager.renew(lease.id);
  await new Promise((r) => setTimeout(r, 800));
  assert.equal(manager.count("copilot.cli"), 1, "renewal extended expiry");
  await new Promise((r) => setTimeout(r, 1500));
  assert.equal(manager.count("copilot.cli"), 0);
  await assert.rejects(send(lease));
  assert.deepEqual(key, Buffer.alloc(32));
  let resolve;
  const pending = new NativeSessions(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const opening = pending.open("copilot.cli");
  const rejected = assert.rejects(opening, /HELD_NATIVE_SESSION/);
  await pending.revoke();
  const otherKey = Buffer.alloc(32, 8);
  resolve({ ...source, context: { ...source.context, identityKey: otherKey } });
  await rejected;
  assert.equal(pending.count("copilot.cli"), 0);
  assert.deepEqual(otherKey, Buffer.alloc(32));
});

test("unfinished HTTP producers cannot delay revocation", async (t) => {
  const { createConnection } = require("node:net");
  const f = await fixture(t),
    lease = await f.open();
  const endpoint = new URL(lease.endpoint);
  const connection = createConnection({
    host: endpoint.hostname,
    port: Number(endpoint.port),
  });
  t.after(() => connection.destroy());
  await new Promise((resolve) => connection.once("connect", resolve));
  connection.write(
    `POST /v1/traces HTTP/1.1\r\nHost: ${endpoint.host}\r\nAuthorization: Bearer ${lease.token}\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n{`,
  );
  const before = performance.now();
  await f.rpc({ operation: "native-close", id: lease.id });
  assert.ok(
    performance.now() - before < 500,
    "no producer-body or upload wait on close",
  );
});

test("native launches are bounded and runtime stop revokes listeners", async (t) => {
  const f = await fixture(t),
    leases = [];
  for (let i = 0; i < 8; i++) leases.push(await f.open());
  await assert.rejects(f.open(), /NATIVE_SESSION_LIMIT/);
  await f.rpc({ operation: "native-close", id: leases[0].id });
  const replacement = await f.open();
  await f.runtime.stop();
  for (const lease of [...leases, replacement])
    await assert.rejects(send(lease));
});

test("a transient renewal failure keeps renewing; a policy hold stops renewal", async (t) => {
  const net = require("node:net");
  const { frame } = require("@traceai/agent-core/client");
  const { runAgent } = require("../dist/src/cli/run.js");
  for (const [failure, expectMore] of [
    [{ durable: false, code: 7, reason_code: "RUNTIME_UNAVAILABLE" }, true],
    [{ durable: false, code: 3, reason_code: "HELD_NATIVE_SESSION" }, false],
  ]) {
    const root = fs.realpathSync(
      fs.mkdtempSync(path.join(os.tmpdir(), "fi-renew-")),
    );
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const socket = path.join(root, "r.sock");
    let renewals = 0;
    const server = net.createServer((c) => {
      let data = Buffer.alloc(0);
      c.on("data", (chunk) => {
        data = Buffer.concat([data, chunk]);
        if (data.length < 4 || data.length < data.readUInt32BE(0) + 4) return;
        const m = JSON.parse(data.subarray(4).toString());
        let reply = { code: 0 };
        if (m.operation === "native-open")
          reply = {
            id: "l",
            token: "t",
            endpoint: "http://127.0.0.1:1/v1/traces",
            renewAfterMs: 40,
          };
        if (m.operation === "native-renew" && renewals++ === 0) reply = failure;
        c.end(frame(reply));
      });
    });
    await new Promise((r) => server.listen(socket, r));
    const adapter = {
      launch: async () => ({
        executable: process.execPath,
        args: ["-e", "setTimeout(() => {}, 400)"],
        env: {},
        native: true,
      }),
    };
    const result = await runAgent(
      adapter,
      { routeRef: "copilot.cli", socketPath: socket },
      [],
    );
    await new Promise((r) => server.close(r));
    assert.equal(result.code, 0);
    if (expectMore)
      assert.ok(renewals > 2, "retried after transient failure: " + renewals);
    else assert.equal(renewals, 1);
  }
});
