// Install only the README's dependencies, then run its exact example offline.
// Usage: node readme.mjs install|run README.md CONSUMER_DIR TARBALL_DIR
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { join, resolve } from "node:path";

const [phase, readmePath, consumerPath, tarballPath] = process.argv.slice(2);
assert.ok(["install", "run"].includes(phase));
const consumer = resolve(consumerPath);
const readme = await readFile(readmePath, "utf8");
const setup = readme.split("## Features")[0].replace(/\\\r?\n\s*/g, " ");
const commands = [...setup.matchAll(/^npm install (.+)$/gm)].map((m) => m[1].split(/\s+/));
const source = setup.match(/```typescript\n([\s\S]*?)\n```/)[1];
const packageName = (specifier) => specifier.startsWith("@")
  ? specifier.split("/").slice(0, 2).join("/").replace(/@[^/]*$/, "")
  : specifier.split("/")[0].split("@")[0];
const dependencies = commands.flat().filter((arg) => !arg.startsWith("-"));
for (const [, specifier] of source.matchAll(/from "([^"]+)"/g)) {
  assert.ok(dependencies.some((dep) => packageName(dep) === packageName(specifier)),
    `README must explicitly install ${packageName(specifier)}`);
}
assert.ok(dependencies.some((dep) => packageName(dep) === "tsx"), "README must install its TypeScript runner");
for (const name of ["FI_API_KEY", "FI_SECRET_KEY", "OPENAI_API_KEY"]) {
  assert.match(setup, new RegExp(`^export ${name}=`, "m"), `README must export ${name}`);
}
const runner = setup.match(/^npx (tsx [\w.-]+\.mts)$/m)?.[1].split(" ");
assert.ok(runner, "README must include an executable ESM TypeScript command");
const env = {
  PATH: process.env.PATH,
  HOME: join(consumer, ".home"),
  TMPDIR: process.env.TMPDIR ?? "/tmp",
  NPM_CONFIG_USERCONFIG: "/dev/null",
  NPM_CONFIG_CACHE: join(consumer, ".npm-cache"),
};

if (phase === "install") {
  await mkdir(consumer, { recursive: true });
  assert.equal((await readdir(consumer)).length, 0, "Install must start in an empty consumer");
  await mkdir(env.HOME);
  const tarballs = await readdir(tarballPath);
  for (const command of commands) {
    // Replace only documented Future AGI dependencies with the candidate release.
    // No preinstalled adapters, runner, peer bypasses or extra dependencies.
    const args = command.map((dep) => {
      if (!dep.startsWith("@traceai/")) return dep;
      const prefix = packageName(dep).slice(1).replace("/", "-") + "-";
      const matches = tarballs.filter((name) => name.startsWith(prefix) && name.endsWith(".tgz"));
      assert.equal(matches.length, 1, `Expected exactly one tarball for ${dep}`);
      return resolve(tarballPath, matches[0]);
    });
    const result = spawnSync("npm", ["install", "--ignore-scripts", "--no-audit", "--no-fund", ...args],
      { cwd: consumer, env, stdio: "inherit" });
    assert.equal(result.status, 0, `README install failed: ${result.error ?? result.signal ?? "npm"}`);
  }
  console.log("PASS: installed only the documented dependencies in an empty consumer");
} else {
  const packedReadme = await readFile(join(consumer, "node_modules/@traceai/langchain/README.md"), "utf8");
  assert.equal(packedReadme, readme, "Run the README that actually ships in the tarball");
  const spans = [];
  const requests = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    requests.push(request.url);
    response.setHeader("content-type", "application/json");
    if (request.url === "/v1/chat/completions") {
      assert.equal(request.headers.authorization, "Bearer synthetic-openai-key");
      assert.equal(body.messages.at(-1).content, "What is OpenTelemetry?");
      response.end(JSON.stringify({
        id: "synthetic-completion", object: "chat.completion", created: 1, model: body.model,
        choices: [{ index: 0, message: { role: "assistant", content: "Synthetic tracing example." }, finish_reason: "stop" }],
        usage: { prompt_tokens: 5, completion_tokens: 4, total_tokens: 9 },
      }));
    } else if (request.url === "/tracer/v1/traces") {
      for (const resource of body.resourceSpans) {
        const attrs = Object.fromEntries(resource.resource.attributes.map((a) => [a.key, a.value.stringValue]));
        assert.equal(attrs["service.name"], "your-project-name");
        for (const scope of resource.scopeSpans) spans.push(...scope.spans);
      }
      response.end("{}");
    } else {
      response.statusCode = 404;
      response.end("{}");
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  try {
    await writeFile(join(consumer, runner[1]), source + "\n");
    const baseUrl = `http://127.0.0.1:${server.address().port}`;
    const child = spawn("npx", runner, { cwd: consumer, env: {
      ...env, FI_API_KEY: "synthetic-fi-key", FI_SECRET_KEY: "synthetic-fi-secret",
      OPENAI_API_KEY: "synthetic-openai-key", FI_BASE_URL: baseUrl, OPENAI_BASE_URL: `${baseUrl}/v1`,
    }, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", (data) => { stdout += data; });
    child.stderr.on("data", (data) => { stderr += data; });
    const watchdog = setTimeout(() => child.kill("SIGKILL"), 30000);
    try {
      const [code, signal] = await once(child, "exit");
      assert.equal(code, 0, `README command failed (${signal}): ${stderr}`);
    } finally { clearTimeout(watchdog); }
    assert.ok(stdout.includes("Synthetic tracing example."));
    assert.deepEqual(requests.sort(), ["/tracer/v1/traces", "/v1/chat/completions"]);
    assert.equal(spans.length, 1, "No missing or duplicate model span");
    assert.equal(spans[0].name, "ChatOpenAI");
    assert.notEqual(spans[0].status?.code, 2);
    assert.ok(BigInt(spans[0].endTimeUnixNano) > BigInt(spans[0].startTimeUnixNano));
    console.log("PASS: exact README runner, real ChatOpenAI, response and completed OTLP span");
  } finally {
    server.closeAllConnections();
    await new Promise((done) => server.close(done));
  }
}
