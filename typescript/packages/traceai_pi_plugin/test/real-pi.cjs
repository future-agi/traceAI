// Cloud-only acceptance: real pinned Pi, packed distributions, local model and OTLP.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { spawn, execFile } = require("node:child_process");
const { promisify } = require("node:util");
const exec = promisify(execFile);
const base = "/work/pi-acceptance";
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
async function main() {
  await fs.mkdir(base, { recursive: true, mode: 0o700 });
  const distributions = {};
  for (const file of await fs.readdir("/work/packs")) {
    const absolute = path.join("/work/packs", file);
    const meta = JSON.parse(
      (await exec("tar", ["-xOf", absolute, "package/package.json"])).stdout,
    );
    distributions[meta.name] = { meta, file, absolute };
  }
  const googleRequests = [];
  const requests = [],
    batches = [],
    jsonRuns = [];
  let hold = false,
    requestCount = 0;
  let acceptedKeys = ["dummy-fi-api", "dummy-fi-secret"];
  let acceptedUserKeys;
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, "http://localhost");
      if (url.pathname.startsWith("/packs/")) {
        res.setHeader("content-type", "application/octet-stream");
        res.end(
          await fs.readFile(
            path.join("/work/packs", path.basename(url.pathname)),
          ),
        );
        return;
      }
      if (url.pathname === "/tracer/v1/traces") {
        let data = "";
        for await (const b of req) data += b;
        const body = JSON.parse(data);
        if (
          ![acceptedKeys, ...(acceptedUserKeys ? [acceptedUserKeys] : [])].some(
            ([api, secret]) =>
              req.headers["x-api-key"] === api &&
              req.headers["x-secret-key"] === secret,
          )
        ) {
          res.writeHead(401, { "content-type": "application/json" });
          res.end("{}");
          return;
        }
        assert.equal(req.headers["x-project-name"], "coding-agents");
        batches.push(body);
        res.setHeader("content-type", "application/json");
        res.end("{}");
        return;
      }
      if (url.pathname.includes(":streamGenerateContent")) {
        let data = "";
        for await (const b of req) data += b;
        googleRequests.push(JSON.parse(data));
        res.writeHead(200, { "content-type": "text/event-stream" });
        res.end(
          "data: " +
            JSON.stringify({
              candidates: [
                {
                  content: {
                    role: "model",
                    parts: [{ text: "Google mock answer" }],
                  },
                  finishReason: "STOP",
                  index: 0,
                },
              ],
              usageMetadata: {
                promptTokenCount: 40,
                candidatesTokenCount: 10,
                totalTokenCount: 50,
              },
              modelVersion: "test",
            }) +
            "\n\n",
        );
        return;
      }
      if (url.pathname === "/v1/chat/completions") {
        let data = "";
        for await (const b of req) data += b;
        const body = JSON.parse(data);
        requests.push(body);
        requestCount++;
        assert.equal(body.model, "test");
        assert.equal(req.headers.authorization, "Bearer dummy-model");
        if (hold) {
          return;
        }
        const msgs = body.messages;
        const last = msgs.at(-1);
        const user = [...msgs].reverse().find((m) => m.role === "user");
        const text =
          typeof user?.content === "string"
            ? user.content
            : JSON.stringify(user?.content);
        if (text.includes("REVIEW_QUEUE_INITIAL") && !text.includes("REVIEW_QUEUE_SECOND") && !text.includes("REVIEW_QUEUE_THIRD")) await delay(400);
        let tool;
        if (last.role !== "tool" && text.includes("TOOL_LOOP"))
          tool = { name: "read", args: { path: "sample.txt" } };
        if (last.role !== "tool" && text.includes("TOOL_FAILURE"))
          tool = { name: "read", args: { path: "missing-file.txt" } };
        if (last.role !== "tool" && text.includes("SUBAGENT"))
          tool = { name: "test_child", args: { task: "child task" } };
        if (tool && !body.tools?.some((t) => t.function?.name === tool.name))
          tool = undefined;
        res.writeHead(200, {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
        });
        const send = (x) =>
          res.write(
            `data: ${JSON.stringify({ id: `mock-${requestCount}`, object: "chat.completion.chunk", created: 1, model: "test", ...x })}\n\n`,
          );
        send({
          choices: [
            {
              index: 0,
              delta: { role: "assistant", content: "" },
              finish_reason: null,
            },
          ],
        });
        if (tool) {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  tool_calls: [
                    {
                      index: 0,
                      id: `call-${requestCount}`,
                      type: "function",
                      function: {
                        name: tool.name,
                        arguments: JSON.stringify(tool.args),
                      },
                    },
                  ],
                },
                finish_reason: null,
              },
            ],
          });
        } else
          send({
            choices: [
              {
                index: 0,
                delta: { content: "A synthetic answer for the tracing test." },
                finish_reason: null,
              },
            ],
          });
        const inputTokens =
          text.includes("COMPACT_NOW") && last.role === "user" ? 90000 : 40;
        send({
          choices: [
            {
              index: 0,
              delta: {},
              finish_reason: tool ? "tool_calls" : "stop",
            },
          ],
          usage: {
            prompt_tokens: inputTokens,
            completion_tokens: 10,
            total_tokens: inputTokens + 10,
            prompt_tokens_details: { cached_tokens: 5 },
          },
        });
        res.end("data: [DONE]\n\n");
        return;
      }
      const name = decodeURIComponent(url.pathname.slice(1));
      const pkg = distributions[name];
      if (pkg) {
        const root = `http://127.0.0.1:${server.address().port}`;
        res.setHeader("content-type", "application/json");
        res.end(
          JSON.stringify({
            name,
            "dist-tags": { latest: pkg.meta.version },
            versions: {
              [pkg.meta.version]: {
                ...pkg.meta,
                dist: { tarball: `${root}/packs/${pkg.file}` },
              },
            },
          }),
        );
        return;
      }
      res.writeHead(302, { location: `https://registry.npmjs.org${req.url}` });
      res.end();
    } catch (error) {
      res.writeHead(500);
      res.end("{}");
      console.error("mock failure", error.message);
    }
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const home = path.join(base, "home"),
    agentDir = path.join(home, "pi-override"),
    cwd = path.join(base, "project");
  await fs.mkdir(agentDir, { recursive: true, mode: 0o700 });
  await fs.mkdir(cwd, { recursive: true, mode: 0o700 });
  await fs.writeFile(
    path.join(cwd, "sample.txt"),
    "Synthetic repository file.",
  );
  const pi = path.resolve(__dirname, "../node_modules/.bin/pi");
  const bin = path.dirname(pi);
  const env = {
    ...process.env,
    HOME: home,
    PI_CODING_AGENT_DIR: agentDir,
    FI_AGENT_CONFIG_DIR: path.join(home, "fi"),
    XDG_STATE_HOME: path.join(home, "state"),
    XDG_CONFIG_HOME: path.join(home, "xdg"),
    PI_OFFLINE: "1",
    PI_SKIP_VERSION_CHECK: "1",
    PI_TELEMETRY: "0",
    MOCK_API_KEY: "dummy-model",
    npm_config_registry: origin,
    npm_config_cache: path.join(home, "npm-cache"),
    PATH: `${bin}:${process.env.PATH}`,
  };
  const version = await exec(pi, ["--version"], { env, timeout: 15000 });
  assert.equal(version.stdout.trim(), "1.5.0");
  // Model calls use only this explicitly configured local provider. No auth cache is copied.
  await fs.writeFile(
    path.join(agentDir, "models.json"),
    JSON.stringify({
      providers: {
        mock: {
          baseUrl: `${origin}/v1`,
          api: "openai-completions",
          apiKey: "$MOCK_API_KEY",
          models: [
            {
              id: "test",
              name: "test",
              reasoning: false,
              input: ["text"],
              cost: { input: 1, output: 2, cacheRead: 0.1, cacheWrite: 1 },
              contextWindow: 100000,
              maxTokens: 1024,
            },
          ],
        },
      },
    }),
  );
  await fs.writeFile(
    path.join(agentDir, "settings.json"),
    JSON.stringify({
      packages: [],
      compaction: { enabled: false },
      retry: { enabled: false },
    }),
  );
  const cliInstall = path.join(base, "cli");
  await fs.mkdir(cliInstall);
  await exec(
    "npm",
    [
      "install",
      "--prefix",
      cliInstall,
      "--ignore-scripts",
      "--legacy-peer-deps",
      "--no-audit",
      "--no-fund",
      ...Object.values(distributions).map((p) => p.absolute),
    ],
    { env, timeout: 120000, maxBuffer: 4e6 },
  );
  const cli = path.join(cliInstall, "node_modules/@traceai/agents/dist/cli.js");
  const setupEnv = {
    ...env,
    FI_API_KEY: "dummy-fi-api",
    FI_SECRET_KEY: "dummy-fi-secret",
    FI_OTLP_ENDPOINT: `${origin}/tracer/v1/traces`,
  };
  const setup = await exec(
    process.execPath,
    [cli, "setup", "pi", "--project", "coding-agents", "--json"],
    { cwd, env: setupEnv, timeout: 120000 },
  );
  assert.equal(JSON.parse(setup.stdout.trim()).registered, true, setup.stdout);
  const repeat = await exec(
    process.execPath,
    [cli, "setup", "pi", "--project", "coding-agents", "--json"],
    { cwd, env: setupEnv, timeout: 120000 },
  );
  assert.equal(JSON.parse(repeat.stdout.trim()).registered, true);
  const settings = JSON.parse(
    await fs.readFile(path.join(agentDir, "settings.json"), "utf8"),
  );
  assert.equal(settings.packages.length, 1);
  assert.equal(settings.compaction.enabled, false);
  assert.equal(
    (await fs.stat(path.join(env.FI_AGENT_CONFIG_DIR, "credentials.json")))
      .mode & 0o777,
    0o600,
  );
  assert.equal(
    (await fs.stat(path.join(agentDir, "futureagi.json"))).mode & 0o777,
    0o600,
  );
  // Enforce loopback-only networking AFTER package installation in every agent/CLI child.
  const guard = path.join(base, "loopback-only.cjs");
  await fs.writeFile(
    guard,
    `const net=require('node:net');const orig=net.Socket.prototype.connect;net.Socket.prototype.connect=function(...args){const a=Array.isArray(args[0])?args[0]:args;const o=typeof a[0]==='object'?a[0]:{host:typeof a[1]==='string'?a[1]:undefined};if(o.path)return orig.apply(this,args);const h=o.host||o.hostname||'localhost';if(!['localhost','127.0.0.1','::1'].includes(h))throw new Error('NON_LOOPBACK_BLOCKED');return orig.apply(this,args)};`,
  );
  env.NODE_OPTIONS = `--require=${guard}`;
  const spans = () =>
    batches.flatMap((b) =>
      b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
    );
  async function run(prompt, extra = [], session = "real-session") {
    const task = exec(
      pi,
      [
        "--offline",
        ...(extra.includes("--approve") ? [] : ["--no-approve"]),
        "--mode",
        "json",
        "-p",
        "--provider",
        "mock",
        "--model",
        "test",
        "--session-id",
        session,
        ...extra,
        prompt,
      ],
      { cwd, env, timeout: 45000, maxBuffer: 8e6 },
    );
    task.child.stdin.end();
    const result = await task;
    const lines = result.stdout
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l));
    jsonRuns.push({
      prompt,
      events: lines.map((e) => e.type),
      stderr: result.stderr,
    });
    assert(
      lines.some(
        (e) => e.type === "message_end" && e.message?.role === "assistant",
      ),
      result.stdout,
    );
    await fs.writeFile(
      "/work/pi-partial.json",
      JSON.stringify(
        { spans: spans(), jsonRuns, providerRequests: requests.length },
        null,
        2,
      ),
    );
    assert(!result.stderr.includes("Failed to load extension"), result.stderr);
    return lines;
  }
  await run("TOOL_LOOP: read the synthetic file and answer.");
  let roots = spans().filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 1, JSON.stringify(jsonRuns));
  let first = spans().filter((s) => s.traceId === roots[0].traceId);
  assert.equal(
    first.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length,
    2,
  );
  assert.equal(
    first.filter((s) => attrs(s)["fi.span.kind"] === "TOOL").length,
    1,
  );
  await run("Second prompt in the resumed conversation.");
  roots = spans().filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 2);
  assert.equal(attrs(roots[0])["session.id"], attrs(roots[1])["session.id"]);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  await run("TOOL_FAILURE: read a missing synthetic file.");
  assert(
    spans().some(
      (s) => attrs(s)["fi.span.kind"] === "TOOL" && s.status.code === 2,
    ),
  );
  // Real extension-defined child boundary: it owns the child request and reports it through the public event bus.
  const childExtension = path.join(base, "child.mjs");
  await fs.writeFile(
    childExtension,
    `export default function(pi){pi.registerTool({name:'test_child',label:'child',description:'Synthetic child test',parameters:{type:'object',properties:{task:{type:'string'}},required:['task']},async execute(id,args){const emit=e=>pi.events.emit('futureagi:subagent:v1',e);emit({type:'start',id:'child',parentToolCallId:id,name:'test.child',input:args.task});emit({type:'request',id:'child-request',parentId:'child',model:'test',provider:'mock',input:args.task});const r=await fetch('${origin}/v1/chat/completions',{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer dummy-model'},body:JSON.stringify({model:'test',messages:[{role:'user',content:'child-only'}],stream:true})});await r.text();emit({type:'end',id:'child-request',result:{output:'Child completed'}});emit({type:'end',id:'child'});return {content:[{type:'text',text:'Child completed'}]}}})}`,
  );
  await run("SUBAGENT: delegate the task.", ["-e", childExtension]);
  const child = spans().find((s) => s.name === "test.child");
  assert(child);
  const spawning = spans().find((s) => s.spanId === child.parentSpanId);
  assert.equal(spawning.name, "test_child");
  assert(
    spans().some(
      (s) =>
        s.parentSpanId === child.spanId && attrs(s)["fi.span.kind"] === "LLM",
    ),
  );
  await require("./native-queue.cjs").run({ pi, cwd, env, spans, requests });
  // Real automatic compaction is within the submitted prompt's settlement boundary.
  const updated = JSON.parse(
    await fs.readFile(path.join(agentDir, "settings.json"), "utf8"),
  );
  updated.compaction = {
    enabled: true,
    reserveTokens: 16384,
    keepRecentTokens: 32,
  };
  await fs.writeFile(
    path.join(agentDir, "settings.json"),
    JSON.stringify(updated),
  );
  const compactOffset = spans().length, compactRequestOffset = requests.length;
  const compactEvents = await run("COMPACT_NOW: summarize this synthetic conversation.");
  const compactSpans = spans().slice(compactOffset);
  const compact = compactSpans.find((s) => s.name === "pi.compaction");
  assert.equal(requests.length - compactRequestOffset, 3, "regular generation plus split summarization requests");
  assert.equal(compactSpans.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 1);
  const sessionFile = path.join(agentDir, "sessions", ...[]);
  const sessionFiles = await fs.readdir(sessionFile, { recursive: true });
  let reported;
  for (const relative of sessionFiles.filter((p) => p.endsWith(".jsonl"))) {
    const entries = (await fs.readFile(path.join(sessionFile, relative), "utf8")).trim().split("\n").map((line) => JSON.parse(line));
    reported = entries.findLast((e) => e.type === "compaction" && e.usage)?.usage ?? reported;
  }
  assert(reported, "native session reports aggregate compaction usage");
  assert.equal(attrs(compact)["gen_ai.usage.input_tokens"], reported.input + reported.cacheRead + reported.cacheWrite);
  assert.equal(attrs(compact)["gen_ai.usage.output_tokens"], reported.output);
  assert.equal(attrs(compact)["gen_ai.cost.total"], reported.cost.total);
  assert.equal(compactSpans.reduce((n, s) => n + (attrs(s)["gen_ai.usage.total_tokens"] || 0), 0), 90110);
  await fs.writeFile("/work/compaction-evidence.json", JSON.stringify({ reported, spans: compactSpans, requests: requests.slice(compactRequestOffset), events: compactEvents }, null, 2));
  assert(
    spans().some(
      (s) =>
        attrs(s)["fi.agent.event"] === "compaction" &&
        attrs(s)["fi.agent.outcome"] === "success",
    ),
  );
  updated.compaction.enabled = false;
  await fs.writeFile(
    path.join(agentDir, "settings.json"),
    JSON.stringify(updated),
  );
  // RPC abort invokes actual host cancellation and awaits shutdown flush.
  const rpc = spawn(
    pi,
    [
      "--offline",
      "--no-approve",
      "--mode",
      "rpc",
      "--provider",
      "mock",
      "--model",
      "test",
      "--session-id",
      "abort-session",
    ],
    { cwd, env, stdio: ["pipe", "pipe", "pipe"] },
  );
  let rpcText = "",
    rpcErr = "";
  const rpcEvents = [];
  rpc.stdout.on("data", (b) => {
    rpcText += b;
    let n;
    while ((n = rpcText.indexOf("\n")) >= 0) {
      const line = rpcText.slice(0, n);
      rpcText = rpcText.slice(n + 1);
      if (line) rpcEvents.push(JSON.parse(line));
    }
  });
  rpc.stderr.on("data", (b) => (rpcErr += b));
  const wait = async (pred) => {
    for (let i = 0; i < 200; i++) {
      if (pred()) return;
      await delay(50);
    }
    throw Error(
      "RPC_TIMEOUT " + JSON.stringify(rpcEvents).slice(-1500) + rpcErr,
    );
  };
  const send = (x) => rpc.stdin.write(JSON.stringify(x) + "\n");
  hold = true;
  const before = requests.length;
  send({ type: "prompt", id: "abort-prompt", message: "Cancel this request." });
  await wait(() => requests.length > before);
  send({ type: "abort", id: "abort" });
  await wait(() => rpcEvents.some((e) => e.type === "agent_settled"));
  hold = false;
  rpc.stdin.end();
  await new Promise((r, j) => {
    rpc.on("exit", r);
    rpc.on("error", j);
  });
  assert(
    spans().some(
      (s) => !s.parentSpanId && attrs(s)["fi.agent.outcome"] === "interrupted",
    ),
  );
  // Metadata-only policy survives fresh process startup.
  const contentBefore = spans().length;
  env.FI_AGENT_CAPTURE_CONTENT = "false";
  await run("PRIVATE_CONTENT", [], "private-session");
  delete env.FI_AGENT_CAPTURE_CONTENT;
  for (const s of spans().slice(contentBefore)) {
    assert.equal(attrs(s)["input.value"], undefined);
    assert.equal(attrs(s)["output.value"], undefined);
  }
  const regressions = {};
  const models = JSON.parse(
    await fs.readFile(path.join(agentDir, "models.json"), "utf8"),
  );
  models.providers["google-mock"] = {
    ...models.providers.mock,
    baseUrl: origin,
    api: "google-generative-ai",
  };
  await fs.writeFile(
    path.join(agentDir, "models.json"),
    JSON.stringify(models),
  );
  const googleOffset = spans().length;
  const googleEvents = await run(
    "GOOGLE_INPUT_MARKER",
    ["--provider", "google-mock", "--system-prompt", "GOOGLE_SYSTEM_MARKER"],
    "google-session",
  );
  assert.equal(
    googleEvents.find(
      (e) => e.type === "message_end" && e.message?.role === "assistant",
    ).message.stopReason,
    "stop",
  );
  assert(
    googleRequests.some((r) =>
      JSON.stringify(r.contents).includes("GOOGLE_INPUT_MARKER"),
    ),
  );
  const googleGeneration = spans()
    .slice(googleOffset)
    .find((s) => s.name === "pi.generation");
  const googleInput = JSON.parse(attrs(googleGeneration)["input.value"]);
  assert(JSON.stringify(googleInput.contents).includes("GOOGLE_INPUT_MARKER"));
  assert(
    JSON.stringify(googleInput.systemInstruction).includes(
      "GOOGLE_SYSTEM_MARKER",
    ),
  );
  assert(JSON.stringify(googleInput.tools).includes("read"));
  assert.equal(googleInput.httpOptions, undefined);
  assert.equal(attrs(googleGeneration)["gen_ai.usage.input_tokens"], 40);
  assert.equal(attrs(googleGeneration)["gen_ai.usage.output_tokens"], 10);
  for (const flag of [
    "FI_HIDE_INPUTS",
    "FI_HIDE_OUTPUTS",
    "FI_AGENT_CAPTURE_CONTENT",
    "FI_HIDE_ERROR_DETAILS",
  ]) {
    const before = spans().length;
    env[flag] = flag === "FI_AGENT_CAPTURE_CONTENT" ? "false" : "true";
    const sessionId = `google-private-${flag}`;
    // Seed actual failed tool history before resuming with the Google provider.
    if (flag === "FI_HIDE_ERROR_DETAILS")
      await run("TOOL_FAILURE: Google hidden history", [], sessionId);
    const offset = spans().length;
    await run(
      "GOOGLE_PRIVATE_MARKER",
      ["--provider", "google-mock"],
      sessionId,
    );
    delete env[flag];
    const privateSpans = spans().slice(offset);
    assert(privateSpans.some((s) => s.name === "pi.generation"));
    assert(
      privateSpans
        .filter((s) => s.name === "pi.generation")
        .every((s) => attrs(s)["input.value"] === undefined),
      flag,
    );
    if (flag === "FI_HIDE_ERROR_DETAILS")
      assert(
        !JSON.stringify(spans().slice(before)).includes(
          "ENOENT: no such file or directory",
        ),
      );
  }
  regressions.googleInputAndPrivacy = true;
  // Same real binary, user installation, external-volume-style project permissions.
  await fs.chmod(cwd, 0o775);
  const externalOffset = spans().length;
  await run("EXTERNAL_VOLUME_PROMPT", [], "external-volume-session");
  assert.equal(
    spans()
      .slice(externalOffset)
      .filter((s) => !s.parentSpanId).length,
    1,
  );
  const externalStatus = await exec(
    process.execPath,
    [cli, "status", "pi", "--json"],
    { cwd, env },
  ).catch((e) => e);
  assert(
    JSON.parse(externalStatus.stdout).issues.includes(
      "PROJECT_CONFIG_IGNORED",
    ),
  );
  await fs.chmod(cwd, 0o700);
  regressions.externalVolume = true;
  // Native installation plus a separately loaded copy must share one observer.
  let offset = spans().length;
  await run(
    "DUPLICATE_CHECK",
    ["-e", path.resolve(__dirname, "../extension.mjs")],
    "duplicate-session",
  );
  let observed = spans().slice(offset);
  assert.equal(observed.filter((s) => !s.parentSpanId).length, 1);
  assert.equal(observed.filter((s) => s.name === "pi.generation").length, 1);
  assert.equal(new Set(observed.map((s) => s.spanId)).size, observed.length);
  regressions.duplicate = true;
  const router = path.join(base, "review-router.mjs");
  await fs.writeFile(
    router,
    `export default function(pi){pi.registerVirtualModel({provider:'review-router',id:'auto',name:'Review Router',contextWindow:100000,maxTokens:1024,route(request,ctx){return {model:ctx.modelRegistry.find('mock','test'),thinkingLevel:request.thinkingLevel}}});}`,
  );
  offset = spans().length;
  const routed = await run(
    "ROUTED_CHECK",
    ["-e", router, "--provider", "review-router", "--model", "auto"],
    "router-session",
  );
  observed = spans()
    .slice(offset)
    .filter((s) => s.name === "pi.generation");
  const actual = routed.find(
    (e) => e.type === "message_end" && e.message?.role === "assistant",
  ).message;
  assert.equal(attrs(observed[0])["gen_ai.request.model"], actual.model);
  assert.equal(attrs(observed[0])["gen_ai.provider.name"], actual.provider);
  assert.equal(actual.model, "test");
  assert.equal(actual.provider, "mock");
  regressions.physicalModel = true;
  const nativeRegressions = require("./native-regressions.cjs");
  await nativeRegressions.routingAndCredentials({
    base,
    cwd,
    env,
    run,
    spans,
    requests,
    regressions,
  });
  for (const [flag, prompt, hidden] of [
    [
      "FI_HIDE_ERROR_DETAILS",
      "TOOL_FAILURE: private-error-check",
      "ENOENT: no such file or directory",
    ],
    [
      "FI_HIDE_OUTPUTS",
      "TOOL_LOOP: hidden-output-check",
      "Synthetic repository file.",
    ],
  ]) {
    offset = spans().length;
    env[flag] = "true";
    const sessionId = `${flag}-session`;
    await run(prompt, [], sessionId);
    // A new process resumes the same history, including failed tool-call IDs.
    await run("Follow up without reading another file.", [], sessionId);
    delete env[flag];
    observed = spans().slice(offset);
    assert(observed.filter((s) => s.name === "pi.generation").length >= 3);
    assert(!JSON.stringify(observed).includes(hidden), flag);
    if (flag === "FI_HIDE_OUTPUTS")
      assert(observed.every((s) => attrs(s)["output.value"] === undefined));
    regressions[flag] = true;
  }
  // A repository edit must not send either auth headers or trace bodies elsewhere,
  // even when credentials have been supplied through the environment.
  const unapproved = [];
  const receiver = http.createServer((q, r) => {
    unapproved.push(q.headers);
    q.resume();
    r.end("{}");
  });
  await new Promise((r) => receiver.listen(0, "127.0.0.1", r));
  const repoConfig = path.join(cwd, ".pi/futureagi.json");
  await fs.mkdir(path.dirname(repoConfig), { recursive: true });
  await fs.writeFile(
    repoConfig,
    JSON.stringify({
      schemaVersion: 1,
      transport: {
        endpoint: `http://127.0.0.1:${receiver.address().port}/tracer/v1/traces`,
      },
    }),
    { mode: 0o600 },
  );
  env.FI_API_KEY = "dummy-fi-api";
  env.FI_SECRET_KEY = "dummy-fi-secret";
  offset = spans().length;
  await run("REPOSITORY_REDIRECT_MUST_NOT_EXPORT", [], "unapproved-session");
  delete env.FI_API_KEY;
  delete env.FI_SECRET_KEY;
  assert.equal(unapproved.length, 0);
  assert.equal(spans().length, offset);
  await fs.unlink(repoConfig);
  receiver.closeAllConnections();
  await new Promise((r) => receiver.close(r));
  regressions.unapprovedReceiver = true;
  const checked = await exec(
    process.execPath,
    [cli, "status", "pi", "--check", "--json"],
    { cwd, env },
  );
  const status = JSON.parse(checked.stdout);
  assert.equal(status.registration, "REGISTERED");
  assert.equal(status.activation, "OBSERVED");
  assert.equal(status.credentialCheck, "ACCEPTED");
  assert(status.delivery.exported > 0);
  assert(status.delivery.lastAcceptedAt);
  assert.deepEqual(status.issues, []);
  const doctor = await exec(
    process.execPath,
    [cli, "doctor", "pi", "--json"],
    { cwd, env },
  );
  assert.deepEqual(JSON.parse(doctor.stdout).issues, []);
  regressions.healthyStatusDoctor = true;
  await nativeRegressions.killWriter(base, env);
  const disable = await exec(
    process.execPath,
    [cli, "disable", "pi", "--json"],
    { cwd, env },
  );
  assert.equal(JSON.parse(disable.stdout).code, "DISABLED");
  const disabledCount = spans().length;
  await run("No telemetry after disable.", [], "disabled-session");
  assert.equal(spans().length, disabledCount);
  for (const root of spans().filter((s) => !s.parentSpanId))
    assert.equal(attrs(root)["gen_ai.usage.input_tokens"], undefined);
  assert(!JSON.stringify(spans()).includes("dummy-fi-secret"));
  assert(!JSON.stringify(spans()).includes("dummy-fi-api"));
  regressions.disableAfterKilledWriter = true;
  // A project profile changes configuration scope, not the existing native user
  // installation. Uninstall must still use the user registration's own receipt.
  await exec(
    process.execPath,
    [cli, "setup", "pi", "--scope", "project", "--configure-only",
      "--project", "coding-agents", "--json"],
    { cwd, env: setupEnv },
  );
  assert.equal(JSON.parse(await fs.readFile(path.join(agentDir, "settings.json"), "utf8")).packages.length, 1);
  await nativeRegressions.killWriter(base, env);
  const removed = await exec(
    process.execPath,
    [cli, "uninstall", "pi", "--json"],
    { cwd, env },
  );
  assert.equal(JSON.parse(removed.stdout).registered, false);
  regressions.uninstallAfterKilledWriter = true;
  const afterRemoval = JSON.parse(
    await fs.readFile(path.join(agentDir, "settings.json"), "utf8"),
  );
  assert.equal(afterRemoval.packages.length, 0);
  assert.equal(afterRemoval.compaction.enabled, false);
  // Exercise project-only setup on the actual Pi package manager, with no user
  // configuration left to mask project-resolution bugs.
  await assert.rejects(fs.stat(path.join(agentDir, "futureagi.json")), { code: "ENOENT" });
  await assert.rejects(fs.stat(path.join(cwd, ".pi/futureagi.json")), { code: "ENOENT" });
  regressions.uninstallAfterProjectConfigureOnly = true;
  await fs.unlink(path.join(env.FI_AGENT_CONFIG_DIR, "credentials.json"));
  const projectSetup = await exec(
    process.execPath,
    [
      cli,
      "setup",
      "pi",
      "--scope",
      "project",
      "--project",
      "coding-agents",
      "--json",
    ],
    { cwd, env: setupEnv, timeout: 120000 },
  );
  assert.equal(JSON.parse(projectSetup.stdout).registered, true);
  // No tracing command may grant Pi project trust. Unapproved removal must stop
  // at Pi's trust boundary, not at an unrelated user-scope ownership receipt.
  await assert.rejects(
    exec(
      process.execPath,
      [cli, "uninstall", "pi", "--scope", "project", "--json"],
      { cwd, env },
    ),
    (error) => JSON.parse(error.stdout).code === "TRUST_REQUIRED",
  );
  assert.equal(
    JSON.parse(await fs.readFile(path.join(cwd, ".pi/settings.json"), "utf8"))
      .packages.length,
    1,
  );
  // Synthetic host fixture representing the user selecting 'Trust' in Pi. This
  // lives only in the disposable sandbox; the adapter never writes trust.json.
  await fs.writeFile(
    path.join(agentDir, "trust.json"),
    JSON.stringify({ [cwd]: true }),
    { mode: 0o600 },
  );
  await exec(process.execPath, [cli, "enable", "pi", "--json"], { cwd, env });
  offset = spans().length;
  await run("PROJECT_SCOPED_TRACE", ["--approve"], "project-session");
  assert.equal(
    spans()
      .slice(offset)
      .filter((s) => !s.parentSpanId).length,
    1,
  );
  const implicitStatus = JSON.parse(
    (
      await exec(process.execPath, [cli, "status", "pi", "--json"], {
        cwd,
        env,
      })
    ).stdout,
  );
  const explicitStatus = JSON.parse(
    (
      await exec(
        process.execPath,
        [cli, "status", "pi", "--project-dir", cwd, "--json"],
        { cwd: base, env },
      )
    ).stdout,
  );
  assert.equal(implicitStatus.enabled, true);
  assert.equal(implicitStatus.registration, "REGISTERED");
  assert.deepEqual(implicitStatus, explicitStatus);
  await exec(process.execPath, [cli, "disable", "pi", "--json"], { cwd, env });
  const enabled = JSON.parse(
    (
      await exec(process.execPath, [cli, "enable", "pi", "--json"], {
        cwd,
        env,
      })
    ).stdout,
  );
  assert.equal(enabled.enabled, true);
  assert.equal(enabled.registration, "REGISTERED");
  const projectUpdate = JSON.parse(
    (
      await exec(
        process.execPath,
        [cli, "update", "pi", "--scope", "project", "--json"],
        { cwd, env },
      )
    ).stdout,
  );
  assert.equal(projectUpdate.registered, true);
  const projectRemoval = JSON.parse(
    (
      await exec(
        process.execPath,
        [
          cli,
          "uninstall",
          "pi",
          "--scope",
          "project",
          "--project-dir",
          cwd,
          "--json",
        ],
        { cwd: base, env },
      )
    ).stdout,
  );
  assert.equal(projectRemoval.registered, false);
  assert.equal(
    JSON.parse(await fs.readFile(path.join(cwd, ".pi/settings.json"), "utf8"))
      .packages.length,
    0,
  );
  regressions.projectLifecycle = true;
  // Persist native qualification independently of the shared CLI's TTY recovery
  // checks below. A later failure must still fail this script, not hide which
  // native scenarios actually completed.
  await fs.writeFile("/work/pi-native-evidence.json", JSON.stringify({
    status: "NATIVE_SCENARIOS_PASS",
    host: "@earendil-works/pi-coding-agent@1.5.0",
    node: process.version,
    roots: spans().filter((s) => !s.parentSpanId).length,
    spans: spans().length,
    providerRequests: requests.length,
    regressions,
  }, null, 2));
  await fs.writeFile("/work/pi-native-otlp.json", JSON.stringify(batches, null, 2));
  // Uninstall removes owned project configuration. Restore the project account
  // explicitly before testing credential recovery against a second user account.
  await exec(
    process.execPath,
    [cli, "setup", "pi", "--scope", "project", "--project",
      "coding-agents", "--configure-only", "--json"],
    { cwd, env: setupEnv },
  );
  // Keep a different, valid user account while revoking the project account.
  acceptedUserKeys = ["user-account-A", "user-account-secret"];
  await exec(
    process.execPath,
    [
      cli,
      "setup",
      "pi",
      "--scope",
      "user",
      "--project",
      "coding-agents",
      "--configure-only",
      "--json",
    ],
    {
      cwd,
      env: {
        ...setupEnv,
        FI_API_KEY: acceptedUserKeys[0],
        FI_SECRET_KEY: acceptedUserKeys[1],
      },
    },
  );
  // Real TTY recovery: rejection offers masked input, failed replacement is atomic.
  const credentialsFile = path.join(
    env.FI_AGENT_CONFIG_DIR,
    "credentials.json",
  );
  const originalCredentials = await fs.readFile(credentialsFile, "utf8");
  acceptedKeys = ["replacement-api", "replacement-secret"];
  const recoveryModes = [
    "cancel",
    "terminate",
    ...Array(10).fill(["invalid", "valid", "paste"]).flat(),
  ];
  for (const mode of recoveryModes) {
    await fs.writeFile(credentialsFile, originalCredentials, { mode: 0o600 });
    await exec(
      "python3",
      [
        "-I",
        path.join(__dirname, "credential-recovery.py"),
        process.execPath,
        cli,
        mode,
      ],
      { cwd, env, timeout: 30000 },
    );
    if (!["valid", "paste"].includes(mode))
      assert.equal(
        await fs.readFile(credentialsFile, "utf8"),
        originalCredentials,
      );
  }
  const replacement = JSON.parse(await fs.readFile(credentialsFile, "utf8"));
  const profile = replacement.profiles[replacement.defaultProfile];
  assert.equal(profile.apiKey, acceptedKeys[0]);
  assert.equal(profile.secretKey, acceptedKeys[1]);
  assert.equal((await fs.stat(credentialsFile)).mode & 0o777, 0o600);
  const userProfile = JSON.parse(
    await fs.readFile(path.join(agentDir, "futureagi.json"), "utf8"),
  ).profile;
  assert.equal(replacement.profiles[userProfile].apiKey, acceptedUserKeys[0]);
  regressions.maskedRecovery = recoveryModes.length;
  regressions.projectAccountRecovery = true;
  // Fresh setup with an explicit project skips readline before masked capture.
  // It must restore an initially non-flowing stdin too, or the CLI never exits.
  for (let n = 0; n < 5; n++) {
    const fresh = path.join(base, `fresh-credentials-${n}`);
    await fs.mkdir(fresh, { mode: 0o700 });
    await exec(
      "python3",
      [
        "-I",
        path.join(__dirname, "credential-recovery.py"),
        process.execPath,
        cli,
        "fresh",
      ],
      {
        cwd: fresh,
        env: {
          ...env,
          FI_AGENT_CONFIG_DIR: path.join(fresh, "config"),
          PI_CODING_AGENT_DIR: path.join(fresh, "agent"),
          FI_OTLP_ENDPOINT: `${origin}/tracer/v1/traces`,
        },
        timeout: 30000,
      },
    );
  }
  regressions.freshMaskedSetup = 5;
  const evidence = {
    regressions,
    node: process.version,
    uninstall: true,
    host: "@earendil-works/pi-coding-agent@1.5.0",
    setup: JSON.parse(setup.stdout),
    repeat: JSON.parse(repeat.stdout),
    roots: spans().filter((s) => !s.parentSpanId).length,
    spans: spans().map((s) => ({
      name: s.name,
      traceId: s.traceId,
      spanId: s.spanId,
      parentSpanId: s.parentSpanId,
      attributes: attrs(s),
      status: s.status,
    })),
    providerRequests: requests.length,
    jsonRuns,
    rpcTypes: rpcEvents.map((e) => e.type),
    disabledCount,
  };
  await fs.writeFile(
    "/work/pi-evidence.json",
    JSON.stringify(evidence, null, 2),
  );
  await fs.writeFile("/work/pi-otlp.json", JSON.stringify(batches, null, 2));
  console.log(
    JSON.stringify({
      PASS: true,
      regressions,
      node: evidence.node,
      uninstall: true,
      compaction: true,
      host: evidence.host,
      roots: evidence.roots,
      spans: evidence.spans.length,
      providerRequests: requests.length,
      realNativeInstall: true,
      jsonPure: true,
      resume: true,
      toolFailure: true,
      subagent: true,
      abort: true,
      disable: true,
    }),
  );
  server.closeAllConnections();
  await new Promise((r) => server.close(r));
}
main().catch((e) => {
  console.error(e);
  process.exit(1);
});
