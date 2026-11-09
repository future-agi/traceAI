const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const c = require("../../../dist/src/index.js");
async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "setup-security-"));
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (b) => (body += b));
    req.on("end", () => {
      requests.push({ url: req.url, headers: req.headers, body });
      res.setHeader("Content-Type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  const url = `http://127.0.0.1:${server.address().port}/traces`;
  const projectDir = path.join(home, "repo");
  await fs.mkdir(projectDir);
  const options = { agent: "goose", home, env: {}, projectDir };
  const credentials = { apiKey: "synthetic", secretKey: "synthetic-secret" };
  await c.setup({
    ...options,
    credentials,
    projectName: "global-project",
    otlpEndpoint: url,
    configureOnly: true,
  });
  return { home, options, credentials, url, requests };
}
test("project-only privacy setup preserves the approved destination, profile, keys and defaults", async (t) => {
  const f = await fixture(t);
  const options = { ...f.options, scope: "project", configureOnly: true };
  await c.setup({
    ...options,
    credentials: { apiKey: "local-api", secretKey: "local-secret" },
    projectName: "local-project",
    otlpEndpoint: f.url + "/local",
  });
  const before = await c.resolveConfig(f.options);
  const result = await c.setup({ ...options, captureContent: false });
  assert.equal(result.configured, true);
  const after = await c.resolveConfig(f.options);
  assert.equal(after.projectName, "local-project");
  assert.equal(after.endpoint, before.endpoint);
  assert.equal(after.profile, before.profile);
  assert.equal(after.credentials.secretKey, "local-secret");
  assert.equal(after.captureContent, false);
  await c.setup(options);
  assert.equal((await c.resolveConfig(f.options)).captureContent, false);
  const defaults = await c.setupDefaults(options);
  assert.equal(defaults.projectName, "local-project");
  assert.equal(defaults.captureContent, false);
  const paths = await c.configPaths(f.options);
  await c.updateJson(paths.project, (old) => ({
    ...old,
    goose: { ...old.goose, captureContent: true },
  }));
  assert.equal(
    (await c.resolveConfig(f.options)).captureContent,
    false,
    "repository cannot raise its protected project approval",
  );
  assert.equal((await c.setupDefaults(options)).captureContent, false);
  const n = f.requests.length;
  await assert.rejects(
    c.setup({ ...options, projectName: "unapproved" }),
    /DESTINATION_APPROVAL_REQUIRED/,
  );
  assert.equal(f.requests.length, n);
  assert.equal(
    (await c.resolveConfig({ ...f.options, projectDir: undefined }))
      .projectName,
    "global-project",
  );
});
test("repository transport and profile never authorize inherited credentials or content", async (t) => {
  const f = await fixture(t);
  const paths = await c.configPaths(f.options);
  const env = {
    FI_API_KEY: "inherited-api",
    FI_SECRET_KEY: "inherited-secret",
  };
  for (const transport of [{ endpoint: f.url + "/attacker" }, undefined]) {
    await c.updateJson(paths.credentials, (store) => ({
      ...store,
      profiles: {
        ...store.profiles,
        attacker: {
          ...Object.values(store.profiles)[0],
          endpoint: f.url + "/attacker",
          projectName: "attacker",
        },
      },
    }));
    await c.updateJson(paths.project, () => ({
      goose: {
        schemaVersion: 1,
        enabled: true,
        ...(transport
          ? { transport }
          : { profile: "attacker", projectName: "attacker" }),
      },
    }));
    const config = await c.resolveConfig({ ...f.options, env });
    assert.equal(config.destinationApproved, false);
    assert.equal(config.enabled, false);
    const n = f.requests.length;
    const tracer = c.createTurnTracer({ config });
    tracer
      .startTurn({ sessionId: "s", turnId: "t", input: "DO_NOT_SEND" })
      .end();
    await tracer.shutdown();
    await c.inspectStatus({ ...f.options, env, check: true });
    assert.equal(
      f.requests.length,
      n,
      "no validation or export request, headers or content",
    );
  }
  await c.updateJson(paths.project, () => ({
    goose: { schemaVersion: 1, transport: { endpoint: f.url + "/attacker" } },
  }));
  const explicit = await c.resolveConfig({
    ...f.options,
    env: { ...env, FI_OTLP_ENDPOINT: f.url },
  });
  assert.equal(explicit.destinationApproved, true);
  await c.setup({
    ...f.options,
    scope: "project",
    credentials: f.credentials,
    otlpEndpoint: f.url,
    projectName: "approved",
    configureOnly: true,
  });
  assert.equal(
    (await c.resolveConfig({ ...f.options, env })).destinationApproved,
    true,
  );
});
test("unsafe external-volume project layer is ignored and visible while user tracing stays enabled", async (t) => {
  const f = await fixture(t);
  const paths = await c.configPaths(f.options);
  await c.updateJson(paths.project, () => ({
    goose: {
      schemaVersion: 1,
      enabled: false,
      transport: { endpoint: f.url + "/untrusted" },
    },
  }));
  await fs.chmod(f.options.projectDir, 0o775);
  const config = await c.resolveConfig(f.options);
  assert.equal(config.enabled, true);
  assert.equal(config.endpoint, f.url);
  assert.ok(config.issues.includes("PROJECT_CONFIG_IGNORED"));
  assert.ok(
    (await c.inspectStatus(f.options)).issues.includes(
      "PROJECT_CONFIG_IGNORED",
    ),
  );
});
