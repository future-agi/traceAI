const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  fixture, payload, spans, attributes, core, fs, path, child,
} = require("./helpers.cjs");
const { CursorObserver, normalizeEvent, cursorAdapter } = require("../dist/index.js");
const { cursorCliAdapter, cursorCliCapabilities } = require("../dist/cli/index.js");
const { register, installRuntime, commands, inspect } = require("../dist/registration.js");

async function transcript(f, prompt = "synthetic prompt") {
  const file = path.join(f.home, ".cursor/projects/conformance/agent-transcripts/session.jsonl");
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  await fs.writeFile(file, [
    { role: "user", message: { content: [{ type: "text", text: `<user_query>${prompt}</user_query>` }] } },
    { role: "assistant", message: { id: "one", content: [{ type: "text", text: "synthetic response" }] } },
    { type: "turn_ended" },
  ].map(JSON.stringify).join("\n") + "\n", { mode: 0o600 });
  return file;
}

for (const [initial, terminal, expected] of [
  [undefined, "terminal-model", "terminal-model"],
  ["initial-model", "terminal-model", "terminal-model"],
  ["initial-model", undefined, "initial-model"],
]) test(`sessionEnd retains source model ${initial} -> ${terminal}`, async (t) => {
  const f = await fixture(t), file = await transcript(f);
  const observer = new CursorObserver(f.config);
  for (const [event, data] of [
    ["beforeSubmitPrompt", { prompt: "synthetic prompt", model: initial }],
    // CLI uses a conversation id here, not the active turn's generation id.
    ["sessionEnd", { generation_id: "session-1", reason: "other", model: terminal, input_tokens: 10, output_tokens: 4 }],
  ]) await observer.accept(normalizeEvent(payload(event, { transcript_path: file, ...data }), event, f.config));
  await observer.close();
  const all = spans(f.requests), root = all.find(s => !s.parentSpanId);
  const generations = all.filter(s => attributes(s)["fi.span.kind"] === "LLM");
  assert.equal(all.length, 2);
  assert.equal(attributes(root)["fi.agent.outcome"], "incomplete");
  assert.equal(attributes(generations[0])["gen_ai.request.model"], expected);
  assert.equal(Number(attributes(generations[0])["gen_ai.usage.input_tokens"]), 10);
  assert.equal(Number(attributes(generations[0])["gen_ai.usage.output_tokens"]), 4);
});

test("sessionEnd never copies session totals across multiple unfinished turns", async (t) => {
  const f = await fixture(t), observer = new CursorObserver(f.config);
  for (const generation_id of ["first", "second"]) {
    await observer.accept(normalizeEvent(payload("beforeSubmitPrompt", { generation_id, prompt: generation_id }), "beforeSubmitPrompt", f.config));
    await observer.accept(normalizeEvent(payload("afterAgentResponse", { generation_id, input_tokens: 10, output_tokens: 4 }), "afterAgentResponse", f.config));
  }
  await observer.accept(normalizeEvent(payload("sessionEnd", { generation_id: "session-1", reason: "other", input_tokens: 100, output_tokens: 40 }), "sessionEnd", f.config));
  await observer.close();
  const roots = spans(f.requests).filter(s => !s.parentSpanId);
  assert.equal(roots.length, 2);
  assert.equal(roots.reduce((n, s) => n + Number(attributes(s)["gen_ai.usage.input_tokens"]), 0), 20);
  assert(observer.gaps.has("AMBIGUOUS_SESSION_USAGE"));
  const malformed = normalizeEvent(payload("sessionEnd", { input_tokens: -1 }), "sessionEnd", f.config);
  assert.equal(malformed.gap, "UNKNOWN_CURSOR_SHAPE");
  assert.equal(malformed.usage, undefined);
});

test("healthy hooks defer activation to core's route and enable-epoch evidence", async (t) => {
  const f = await fixture(t), runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  const options = { ...f.options, adapter: cursorAdapter };
  let status = await core.inspectStatus(options);
  assert.equal(status.exitCode, 2);
  assert.equal(status.activation, "NOT_OBSERVED");
  assert(status.issues.includes("ACTIVATION_NOT_OBSERVED"));
  assert(!status.issues.includes("GUI_ACTIVATION_NOT_OBSERVED"));
  const file = await transcript(f);
  const hook = commands(runtime, f.config.paths.configDir);
  for (const [event, extra] of [
    ["beforeSubmitPrompt", { prompt: "synthetic prompt" }],
    ["stop", { status: "completed", model: "test-model", input_tokens: 10, output_tokens: 4 }],
  ]) {
    const result = await child(hook[event].command, payload(event, { transcript_path: file, ...extra }), { HOME: f.home, PATH: "/missing" });
    assert.equal(result.code, 0);
  }
  status = await core.inspectStatus(options);
  assert.equal(status.finisher.last.result, "EXPORTED_FULL");
  assert.equal(status.activation, "OBSERVED");
  assert.equal(status.exitCode, 0, JSON.stringify(status));
  // A different destination must not inherit successful delivery evidence.
  status = await core.inspectStatus({ ...options, flags: { projectName: "different-route" } });
  assert.equal(status.activation, "NOT_OBSERVED");
  assert.equal(status.exitCode, 2);
  // Neither an earlier finisher receipt nor an earlier epoch proves activation.
  await core.disable(f.options);
  await core.enable(f.options);
  status = await core.inspectStatus(options);
  assert.equal(status.activation, "NOT_OBSERVED");
  assert.equal(status.exitCode, 2);
  await fs.unlink(path.join(runtime, "hook.cjs"));
  status = await core.inspectStatus(options);
  assert(status.issues.includes("CURSOR_RUNTIME_MISSING"));
});

test("CLI setup owns one shared-hook lifecycle, including disable, reconfigure and uninstall", async (t) => {
  const f = await fixture(t);
  const bin = path.join(f.home, "bin");
  await fs.mkdir(bin, { mode: 0o700 });
  await fs.writeFile(path.join(bin, "cursor-agent"), `#!${process.execPath}\nconsole.log(process.argv.includes('--version') ? '2026.11.05-86047df' : 'Start the Cursor Agent --output-format');\n`, { mode: 0o700 });
  const oldPath = process.env.PATH;
  process.env.PATH = bin;
  t.after(() => { process.env.PATH = oldPath; });
  const options = { ...f.options, agent: "cursor-cli", adapter: cursorCliAdapter };
  const setup = await core.setup(options);
  assert.equal(setup.code, "REGISTERED", JSON.stringify(setup));
  const cli = await core.resolveConfig(options);
  const ide = await core.resolveConfig(f.options);
  assert.equal(cli.agent, ide.agent, "shared hook identity must not fork by CLI alias");
  assert.equal(cli.generation, ide.generation);
  assert.equal(core.deliveryStatePath(cli), core.deliveryStatePath(ide));
  assert.equal(cursorCliCapabilities.qualified, true);
  assert.equal(cursorCliCapabilities.print.liveQualified, false);
  assert.equal(await cursorCliAdapter.run({ config: cli, scope: "user" }, []), 2);
  const file = await transcript(f);
  const hooks = JSON.parse(await fs.readFile(path.join(f.home, ".cursor/hooks.json"), "utf8")).hooks;
  for (const entries of Object.values(hooks)) assert.equal(entries.length, 1);
  const send = async (event, data) => {
    const result = await child(hooks[event][0].command, payload(event, {
      cursor_version: "2026.11.05-86047df", transcript_path: file, ...data,
    }), { HOME: f.home, PATH: bin });
    assert.equal(result.code, 0);
    assert.equal(result.stderr, "");
  };
  await send("beforeSubmitPrompt", { prompt: "synthetic prompt" });
  await send("stop", { status: "completed", model: "cli-model", input_tokens: 10, output_tokens: 4 });
  assert.equal(spans(f.requests).length, 2);
  assert.equal((await core.inspectStatus(options)).exitCode, 0);
  assert.equal((await core.inspectStatus({ ...f.options, adapter: cursorAdapter })).exitCode, 0);
  await core.disable(options);
  assert.equal(core.collectionEnabled(cli), false);
  assert.equal(core.collectionEnabled(ide), false);
  await core.enable(options);
  assert.equal(core.collectionEnabled(cli), false, "enable cannot revive a stale snapshot");
  assert.equal((await core.inspectStatus(options)).activation, "NOT_OBSERVED");
  const fresh = await core.resolveConfig(f.options);
  assert.equal(core.collectionEnabled(fresh), true);
  await core.setup({ ...options, captureContent: false });
  assert.equal(core.collectionEnabled(fresh), false, "CLI privacy reconfiguration fences existing IDE snapshots");
  assert.equal((await core.resolveConfig(f.options)).captureContent, false);
  const manifest = await core.readJson(cli.paths.manifest);
  assert.equal(manifest.registrations.cursor.status, "REGISTERED");
  assert.equal(manifest.registrations["cursor-cli"], undefined);
  // A different agent remains independently configured and enabled.
  await core.setup({ ...f.options, agent: "pi", configureOnly: true });
  const other = await core.resolveConfig({ ...f.options, agent: "pi" });
  await core.uninstall(options);
  assert.equal((await inspect(f.context)).registered, false);
  await assert.rejects(fs.stat(cli.paths.user), { code: "ENOENT" });
  assert.equal(core.collectionEnabled(other), true);
});
