const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const c = require("../../../dist/src/index.js");

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "pi-scope-"));
  const projectDir = path.join(home, "repo");
  await fs.mkdir(projectDir);
  const requests = [];
  let rejected;
  const server = http.createServer((q, r) => {
    q.resume();
    requests.push({
      api: q.headers["x-api-key"],
      project: q.headers["x-project-name"],
    });
    r.writeHead(q.headers["x-api-key"] === rejected ? 401 : 200, {
      "content-type": "application/json",
    });
    r.end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.chmod(projectDir, 0o700);
    await fs.rm(home, { recursive: true, force: true });
  });
  const options = {
    agent: "pi",
    home,
    env: {},
    projectDir,
    configureOnly: true,
  };
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const seed = async (
    scope,
    apiKey,
    projectName = "same",
    captureContent = true,
  ) =>
    c.setup({
      ...options,
      scope,
      otlpEndpoint: endpoint,
      projectName,
      captureContent,
      credentials: { apiKey, secretKey: `${apiKey}-secret` },
    });
  return {
    home,
    projectDir,
    options,
    endpoint,
    requests,
    seed,
    reject: (value) => (rejected = value),
  };
}

test("project setup reuses its bound account, defaults and privacy, never the user account", async (t) => {
  const f = await fixture(t);
  await f.seed("user", "account-A");
  await f.seed("project", "account-B", "same", false);
  assert.equal(
    (await c.setup({ ...f.options, scope: "project" })).configured,
    true,
  );
  assert.deepEqual(
    f.requests.map((r) => r.api),
    ["account-A", "account-B", "account-B"],
  );
  let config = await c.resolveConfig(f.options);
  assert.equal(config.credentials.apiKey, "account-B");
  assert.equal(config.captureContent, false);
  const userBefore = await c.readSettings(config.paths.user);
  // Changing project defaults requires an explicit approval, then those defaults persist.
  await c.setup({
    ...f.options,
    scope: "project",
    projectName: "project-only",
    approveDestination: true,
  });
  const selected = await c.selectSetupProfile({
    ...f.options,
    scope: "project",
  });
  assert.equal(selected.saved.projectName, "project-only");
  await c.setup({ ...f.options, scope: "project" });
  assert.equal(f.requests.at(-1).project, "project-only");
  assert.equal(f.requests.at(-1).api, "account-B");
  assert.deepEqual(await c.readSettings(config.paths.user), userBefore);
  // Explicit user setup is not influenced by the current repo's profile.
  await c.setup({ ...f.options, scope: "user" });
  assert.equal(f.requests.at(-1).api, "account-A");
});

test("revoked project credentials are rejected atomically without trying the valid user profile", async (t) => {
  const f = await fixture(t);
  await f.seed("user", "account-A");
  await f.seed("project", "account-B");
  const paths = await c.configPaths(f.options);
  const before = await fs.readFile(paths.credentials, "utf8");
  f.reject("account-B");
  const result = await c.setup({ ...f.options, scope: "project" });
  assert.equal(result.code, "AUTH_REJECTED");
  assert.equal(f.requests.at(-1).api, "account-B");
  assert.equal(await fs.readFile(paths.credentials, "utf8"), before);
  await c.setup({
    ...f.options,
    scope: "project",
    credentials: { apiKey: "account-C", secretKey: "secret-C" },
  });
  assert.equal(
    (await c.resolveConfig(f.options)).credentials.apiKey,
    "account-C",
  );
  assert.equal(
    (await c.selectSetupProfile({ ...f.options, scope: "user" })).saved.apiKey,
    "account-A",
  );
});

test("setup refuses transplanted profiles and repository-changed destinations before validation", async (t) => {
  const f = await fixture(t);
  await f.seed("user", "account-A");
  await f.seed("project", "account-B");
  const paths = await c.configPaths(f.options);
  const projectText = await fs.readFile(paths.project, "utf8");
  const other = path.join(f.home, "other");
  await fs.mkdir(other);
  const otherPaths = await c.configPaths({ ...f.options, projectDir: other });
  await c.writeProtected(otherPaths.project, projectText);
  const before = f.requests.length;
  await assert.rejects(
    c.setup({ ...f.options, projectDir: other, scope: "project" }),
    /DESTINATION_APPROVAL_REQUIRED/,
  );
  await c.updateJson(paths.project, (p) => ({
    ...p,
    transport: { endpoint: "https://unapproved.invalid/traces" },
  }));
  await assert.rejects(
    c.setup({ ...f.options, scope: "project" }),
    /DESTINATION_APPROVAL_REQUIRED/,
  );
  await c.writeProtected(paths.project, projectText);
  const user = await c.readSettings(paths.user);
  await c.updateJson(paths.project, (p) => ({ ...p, profile: user.profile }));
  await assert.rejects(
    c.setup({ ...f.options, scope: "project" }),
    /DESTINATION_APPROVAL_REQUIRED/,
  );
  assert.equal(f.requests.length, before);
});

test("unsafe external-volume project layer falls back to user and surfaces a diagnostic", async (t) => {
  const f = await fixture(t);
  await f.seed("user", "account-A");
  const paths = await c.configPaths(f.options);
  await c.writeProtected(
    paths.project,
    JSON.stringify({
      schemaVersion: 1,
      transport: { endpoint: "https://unapproved.invalid/traces" },
    }),
  );
  await fs.chmod(f.projectDir, 0o775);
  const config = await c.resolveConfig(f.options);
  assert.equal(config.enabled, true);
  assert.equal(config.credentials.apiKey, "account-A");
  assert.equal(config.endpoint, f.endpoint);
  assert(config.issues.includes("PROJECT_CONFIG_IGNORED"));
  assert(
    (await c.inspectStatus(f.options)).issues.includes(
      "PROJECT_CONFIG_IGNORED",
    ),
  );
  // Explicit writes to unsafe project directories still fail, never weakening the store.
  await assert.rejects(
    c.setup({ ...f.options, scope: "project" }),
    /UNSAFE_DIRECTORY_MODE/,
  );
});
