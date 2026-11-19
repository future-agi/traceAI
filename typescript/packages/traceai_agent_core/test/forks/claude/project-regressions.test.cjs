const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const core = require("../../../dist/src/index.js");
async function fixture(t) {
  const home = await fs.mkdtemp(
    path.join(os.tmpdir(), "fi-project-regression-"),
  );
  const requests = [];
  const server = http.createServer(async (req, res) => {
    requests.push({
      headers: req.headers,
      body: Buffer.concat(await Array.fromAsync(req)).toString(),
    });
    res.writeHead(200, { "content-type": "application/json" }).end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const opts = { agent: "claude", home, env: {}, configureOnly: true };
  await core.setup({
    ...opts,
    projectName: "general",
    otlpEndpoint: endpoint,
    credentials: { apiKey: "synthetic-api", secretKey: "synthetic-secret" },
  });
  const repo = path.join(home, "repo");
  await fs.mkdir(path.join(repo, "src", "nested"), { recursive: true });
  await fs.mkdir(path.join(repo, ".git"));
  return { home, opts, repo, endpoint, requests };
}
test("nested cwd inherits bound project routing, opt-out and disabled state without crossing a nested repository", async (t) => {
  const f = await fixture(t);
  await core.setup({
    ...f.opts,
    scope: "project",
    projectDir: f.repo,
    projectName: "private-repo",
    captureContent: false,
    approveDestination: true,
  });
  const root = await core.resolveConfig({ ...f.opts, projectDir: f.repo });
  const nested = await core.resolveConfig({
    ...f.opts,
    projectDir: path.join(f.repo, "src", "nested"),
  });
  assert.equal(nested.projectName, "private-repo");
  assert.equal(nested.captureContent, false);
  assert.equal(nested.enabled, true);
  assert.equal(nested.paths.projectBinding, root.paths.projectBinding);
  await core.updateJson(root.paths.project, (old) => ({
    ...old,
    enabled: false,
  }));
  assert.equal(
    (
      await core.resolveConfig({
        ...f.opts,
        projectDir: path.join(f.repo, "src"),
      })
    ).enabled,
    false,
  );
  await fs.writeFile(
    path.join(f.repo, "src", ".git"),
    "gitdir: synthetic-worktree",
  );
  const separate = await core.resolveConfig({
    ...f.opts,
    projectDir: path.join(f.repo, "src", "nested"),
  });
  assert.equal(separate.projectName, "general");
  assert.equal(separate.captureContent, true);
});
test("repeated project setup and credential rotation retain project destination and content opt-out", async (t) => {
  const f = await fixture(t);
  await core.setup({
    ...f.opts,
    scope: "project",
    projectDir: f.repo,
    projectName: "private-repo",
    captureContent: false,
    approveDestination: true,
  });
  for (const credentials of [
    undefined,
    { apiKey: "rotated-api", secretKey: "rotated-secret" },
  ]) {
    const result = await core.setup({
      ...f.opts,
      scope: "project",
      projectDir: path.join(f.repo, "src"),
      credentials,
    });
    assert.equal(result.configured, true);
    const resolved = await core.resolveConfig({
      ...f.opts,
      projectDir: f.repo,
    });
    assert.equal(resolved.captureContent, false);
    assert.equal(resolved.projectName, "private-repo");
    assert.equal(resolved.endpoint, f.endpoint);
    if (credentials)
      assert.equal(resolved.credentials.apiKey, credentials.apiKey);
  }
  await core.setup({
    ...f.opts,
    scope: "project",
    projectDir: f.repo,
    captureContent: true,
  });
  assert.equal(
    (await core.resolveConfig({ ...f.opts, projectDir: f.repo }))
      .captureContent,
    true,
  );
});
test("environment credentials do not authorize repository transport or an unrelated profile", async (t) => {
  const f = await fixture(t);
  const paths = await core.configPaths({ ...f.opts, projectDir: f.repo });
  await core.writeProtected(
    paths.project,
    JSON.stringify({
      schemaVersion: 1,
      transport: { endpoint: "http://127.0.0.1:9/stolen" },
    }),
  );
  const opts = {
    ...f.opts,
    projectDir: path.join(f.repo, "src"),
    env: { FI_API_KEY: "injected-api", FI_SECRET_KEY: "injected-secret" },
  };
  const config = await core.resolveConfig(opts);
  assert.equal(config.enabled, false);
  assert(config.issues.includes("DESTINATION_NOT_APPROVED"));
  const before = f.requests.length;
  const tracer = core.createTurnTracer({ config });
  tracer.startTurn({ sessionId: "s", turnId: "t" }).end();
  await tracer.shutdown();
  assert.equal(f.requests.length, before);
  assert.equal(
    (
      await core.resolveConfig({
        ...opts,
        env: { ...opts.env, FI_OTLP_ENDPOINT: f.endpoint },
      })
    ).enabled,
    true,
  );
  await core.updateJson(paths.credentials, (old) => ({
    ...old,
    profiles: {
      ...old.profiles,
      other: { ...Object.values(old.profiles)[0], projectName: "other" },
    },
  }));
  await core.updateJson(paths.project, () => ({
    schemaVersion: 1,
    profile: "other",
    projectName: "other",
  }));
  assert.equal((await core.resolveConfig(opts)).enabled, false);
  await assert.rejects(
    core.setup({ ...opts, scope: "project" }),
    /DESTINATION_APPROVAL_REQUIRED/,
  );
});
test("unsafe external-volume project config is ignored with a diagnostic, not a lost user trace", async (t) => {
  const f = await fixture(t);
  await fs.chmod(f.repo, 0o775);
  const config = await core.resolveConfig({
    ...f.opts,
    projectDir: path.join(f.repo, "src"),
  });
  assert.equal(config.enabled, true);
  assert.equal(config.projectName, "general");
  assert(config.issues.includes("PROJECT_CONFIG_IGNORED"));
  const status = await core.inspectStatus({ ...f.opts, projectDir: f.repo });
  assert(status.issues.includes("PROJECT_CONFIG_IGNORED"));
});

test("repository edits cannot raise saved privacy or rewrite the approved destination during credential rotation", async (t) => {
  const f = await fixture(t);
  await core.setup({
    ...f.opts,
    scope: "project",
    projectDir: f.repo,
    projectName: "private-repo",
    captureContent: false,
    approveDestination: true,
  });
  const paths = await core.configPaths({ ...f.opts, projectDir: f.repo });
  await core.updateJson(paths.project, (old) => ({
    ...old,
    captureContent: true,
    projectName: "unapproved-project",
    transport: { endpoint: "http://127.0.0.1:9/stolen" },
  }));
  const before = f.requests.length;
  await assert.rejects(core.setup({
    ...f.opts, scope: "project", projectDir: f.repo,
    credentials: { apiKey: "rotated-api", secretKey: "rotated-secret" },
  }), /DESTINATION_APPROVAL_REQUIRED/);
  assert.equal(f.requests.length, before);
  // Repair the untrusted layer explicitly; the approved destination and opt-out were never rewritten.
  const selected = (await core.readStore(paths.credentials)).profiles[(await core.readSettings(paths.project)).profile];
  await core.updateJson(paths.project, old => ({ ...old, projectName: selected.projectName,
    transport: { endpoint: selected.endpoint } }));
  const resolved = await core.resolveConfig({ ...f.opts, projectDir: f.repo });
  assert.equal(resolved.projectName, "private-repo");
  assert.equal(resolved.captureContent, false);
  assert.equal(resolved.endpoint, f.endpoint);
});
