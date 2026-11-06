const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const core = require("@traceai/agent-core");
const { deliveryStatePath } = require("@traceai/agent-core/config");
const { capture } = require("../dist/capture.js");
const plugin = require("../dist");
const { text } = require("../dist/events.js");

async function fixture(t, overrides = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "grok-unified-"));
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      requests.push(JSON.parse(body));
      res.setHeader("Content-Type", "application/json");
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const options = { agent: "grok", home, env: {} };
  const setup = {
    ...options,
    credentials: { apiKey: "test-api", secretKey: "test-secret" },
    projectName: "grok-unified",
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    configureOnly: true,
  };
  await core.setup(setup);
  const config = Object.assign(await core.resolveConfig(options), overrides);
  assert.equal(config.enabled, true);
  assert.equal(config.destinationApproved, true);
  assert.ok(config.credentials);
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  return { home, options, setup, config, spans: () => requests.flatMap((r) => r.resourceSpans.flatMap((rs) => rs.scopeSpans.flatMap((ss) => ss.spans))) };
}
const attrs = (s) => Object.fromEntries((s.attributes ?? []).map((a) => [a.key, Object.values(a.value)[0]]));
const event = (event, extra = {}) => ({ event, sessionId: "session", promptId: "prompt", time: 1000, outcome: "success", ...extra });
const row = (update, meta = {}) => ({ params: { sessionId: "session", update, _meta: { promptId: "prompt", ...meta } } });
function generation(observer, index, output) {
  observer.transcript(row({ sessionUpdate: "agent_message_chunk", content: { text: output } }, { streamStartMs: 1100 + index * 100, agentTimestampMs: 1120 + index * 100, eventId: `event-${index}` }));
}

test("Grok generations are LLMs with partial inputs, tool ids and one last-generation usage owner", async (t) => {
  const f = await fixture(t), tracer = core.createTurnTracer({ config: f.config }), observer = new plugin.GrokObserver(tracer, f.config);
  observer.handle(event("UserPromptSubmit", { input: "Synthetic prompt" }));
  generation(observer, 0, "Calling a tool");
  observer.handle(event("PostToolUse", { toolId: "call-1", toolName: "read_file", input: "sample.txt", output: "contents", time: 1150 }));
  observer.transcript(row({ sessionUpdate: "tool_call_update", status: "completed", rawOutput: "contents" }));
  generation(observer, 1, "Final answer");
  observer.handle(event("Stop", { time: 1500 }));
  observer.transcript(row({ sessionUpdate: "turn_completed", prompt_id: "prompt", stop_reason: "end_turn", usage: { inputTokens: 80, outputTokens: 20, cost_usd_ticks: 999 } }));
  observer.finish("session");
  await tracer.flush();
  await tracer.shutdown();
  const all = f.spans(), root = all.find((s) => !s.parentSpanId), models = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(all.length, 4);
  assert.equal(models.length, 2);
  assert.equal(attrs(models[0])["input.value"], "Synthetic prompt");
  assert.match(attrs(models[1])["input.value"], /contents/);
  assert.equal(attrs(models[0])["fi.agent.timing"], "reconstructed");
  assert.equal(attrs(models[0])["fi.agent.input.completeness"], "partial");
  const owners = all.filter((s) => attrs(s)["gen_ai.usage.total_tokens"] !== undefined);
  assert.equal(owners.length, 1);
  assert.equal(attrs(owners[0])["output.value"], "Final answer");
  assert.equal(attrs(owners[0])["gen_ai.usage.total_tokens"], 100);
  assert.equal(attrs(root)["gen_ai.usage.total_tokens"], undefined);
  const tool = all.find((s) => attrs(s)["fi.span.kind"] === "TOOL");
  assert.equal(attrs(tool)["tool.name"], "read_file");
  assert.equal(attrs(tool)["tool_call.id"], "call-1");
  assert.ok(all.every((s) => attrs(s)["gen_ai.cost.total"] === undefined && attrs(s)["gen_ai.server.time_to_first_token"] === undefined));
});

test("a source turn without generations owns additive usage on its root", async (t) => {
  const f = await fixture(t), tracer = core.createTurnTracer({ config: f.config }), observer = new plugin.GrokObserver(tracer, f.config);
  observer.handle(event("UserPromptSubmit"));
  observer.handle(event("Stop", { time: 1400 }));
  observer.transcript(row({ sessionUpdate: "turn_completed", prompt_id: "prompt", usage: { inputTokens: 5, outputTokens: 2 } }));
  observer.finish("session");
  await tracer.flush();
  await tracer.shutdown();
  assert.equal(f.spans().length, 1);
  assert.equal(attrs(f.spans()[0])["gen_ai.usage.total_tokens"], 7);
});

test("fatal model failure makes the root ERROR and hides failure content", async (t) => {
  const f = await fixture(t, { hideErrorDetails: true }), tracer = core.createTurnTracer({ config: f.config }), observer = new plugin.GrokObserver(tracer, f.config);
  observer.handle(event("UserPromptSubmit"));
  generation(observer, 0, "private model failure body");
  observer.handle(event("StopFailure", { time: 1500, outcome: "error", output: "private model failure body" }));
  observer.finish("session");
  await tracer.flush();
  await tracer.shutdown();
  assert.equal(f.spans().length, 2);
  assert.ok(f.spans().every((s) => s.status.code === 2));
  assert.doesNotMatch(JSON.stringify(f.spans()), /private model failure body/);
});

test("failed tool-result envelopes cannot leak into the next generation input", async (t) => {
  const f = await fixture(t, { hideErrorDetails: true }), tracer = core.createTurnTracer({ config: f.config }), observer = new plugin.GrokObserver(tracer, f.config);
  observer.handle(event("UserPromptSubmit", { input: "prompt" }));
  const payload = { isError: true, message: "PRIVATE-FAILURE-BODY" };
  const tool = plugin.normalizeHook({ hookEventName: "PostToolUse", sessionId: "session", promptId: "prompt", toolUseId: "tool", toolName: "read_file", toolResult: payload }, f.config);
  assert.equal(tool.outcome, "error");
  assert.equal(tool.output, undefined);
  observer.handle(tool);
  observer.transcript(row({ sessionUpdate: "tool_call_update", status: "completed", rawOutput: payload }));
  generation(observer, 0, "Recovered answer");
  observer.handle(event("Stop", { time: 1500 }));
  observer.transcript(row({ sessionUpdate: "turn_completed", prompt_id: "prompt", stop_reason: "end_turn" }));
  observer.finish("session");
  await tracer.flush();
  await tracer.shutdown();
  assert.equal(f.spans().length, 3);
  assert.doesNotMatch(JSON.stringify(f.spans()), /PRIVATE-FAILURE-BODY/);
  assert.notEqual(f.spans().find((s) => !s.parentSpanId).status.code, 2);
});

test("successful persisted completion recovers an earlier failed model attempt without leaking error context", async (t) => {
  const f = await fixture(t, { hideErrorDetails: true }), tracer = core.createTurnTracer({ config: f.config }), observer = new plugin.GrokObserver(tracer, f.config);
  observer.handle(event("UserPromptSubmit"));
  generation(observer, 0, '{"isError":true,"message":"retry"}');
  observer.handle(event("StopFailure", { time: 1150, outcome: "error" }));
  generation(observer, 1, "Recovered answer");
  observer.handle(event("Stop", { time: 1500 }));
  observer.transcript(row({ sessionUpdate: "turn_completed", prompt_id: "prompt", stop_reason: "end_turn", usage: { inputTokens: 9, outputTokens: 3 } }));
  observer.finish("session");
  await tracer.flush();
  await tracer.shutdown();
  const all = f.spans();
  assert.equal(all.length, 3);
  assert.equal(all.filter((s) => s.status.code === 2).length, 1);
  assert.notEqual(all.find((s) => !s.parentSpanId).status.code, 2);
  assert.doesNotMatch(JSON.stringify(all), /retry/);
});

test("missing terminal completion remains incomplete rather than a successful Stop", async (t) => {
  const f = await fixture(t), tracer = core.createTurnTracer({ config: f.config }), observer = new plugin.GrokObserver(tracer, f.config);
  observer.handle(event("UserPromptSubmit"));
  observer.handle(event("Stop", { time: 1500 }));
  observer.finish("session");
  await tracer.flush();
  await tracer.shutdown();
  assert.equal(attrs(f.spans()[0])["fi.agent.outcome"], "incomplete");
  assert.equal(attrs(f.spans()[0])["fi.agent.gap"], "MISSING_TURN_COMPLETION");
});

test("redaction precedes truncation including escaped/nested aliases and Basic auth", () => {
  const config = { hideInputs: false, hideOutputs: false, hideErrorDetails: false, credentials: { apiKey: "known-boundary-secret", secretKey: "another-secret" } };
  for (const input of [
    { "api-key": "private-value", padding: "x".repeat(25000) },
    { wrapper: JSON.stringify({ accessToken: 'secret-with-"-quote', rest: "tail" }) },
    'Authorization: Basic c2VjcmV0LXZhbHVl',
    "x".repeat(19990) + "known-boundary-secret",
  ]) {
    const result = text(input, config);
    assert.doesNotMatch(result, /private-value|secret-with|c2VjcmV0|known-bound/);
    assert.ok(result.length <= 20000);
  }
});

test("newer releases in qualified Grok major line remain supported", () => {
  for (const version of ["1.0.77", "1.0.78", "1.2.0"]) assert.equal(plugin.supportedHostVersion(version), true);
  for (const version of ["1.0.76", "2.0.0", "unknown", "1.0.78-beta", undefined]) assert.equal(plugin.supportedHostVersion(version), false);
});

test("unsafe and malformed project layers fall back with explicit diagnostics", async (t) => {
  const f = await fixture(t), project = path.join(f.home, "project"), layer = path.join(project, ".grok");
  await fs.mkdir(layer, { recursive: true, mode: 0o700 });
  await fs.writeFile(path.join(layer, "futureagi.json"), "{broken", { mode: 0o600 });
  const malformed = await core.resolveConfig({ ...f.options, projectDir: project });
  assert.equal(malformed.projectName, f.config.projectName);
  assert.ok(malformed.issues.includes("PROJECT_CONFIG_IGNORED"));
  await fs.writeFile(path.join(layer, "futureagi.json"), '{}');
  await fs.chmod(project, 0o775);
  const unsafe = await core.resolveConfig({ ...f.options, projectDir: project });
  assert.equal(unsafe.projectName, f.config.projectName);
  assert.ok(unsafe.issues.includes("PROJECT_CONFIG_IGNORED"));
});

test("scoped activation receipts and serialized hook updates survive overlap and setup fencing", async (t) => {
  const f = await fixture(t);
  await capture(event("SessionStart"), f.config);
  const receipt = JSON.parse(await fs.readFile(deliveryStatePath(f.config), "utf8"));
  assert.equal(receipt.binding, f.config.deliveryBinding);
  assert.ok(receipt.activatedAt);
  await Promise.all(Array.from({ length: 6 }, (_, index) => capture(event("PreToolUse", { toolId: `call-${index}` }), f.config)));
  const parent = path.join(f.config.paths.configDir, "grok-state");
  const dir = path.join(parent, (await fs.readdir(parent))[0]);
  const state = JSON.parse(await fs.readFile(path.join(dir, (await fs.readdir(dir)).find((p) => p.endsWith(".json"))), "utf8"));
  assert.equal(state.events.filter((e) => e.event === "PreToolUse").length, 6);
  await core.setup(f.setup);
  const next = await core.resolveConfig(f.options);
  assert.notEqual(next.generation, f.config.generation);
  assert.notEqual(deliveryStatePath(next), deliveryStatePath(f.config));
  const oldTracer = core.createTurnTracer({ config: f.config });
  oldTracer.startTurn({ sessionId: "stale", turnId: "old" }).end();
  await oldTracer.shutdown();
  assert.equal(f.spans().length, 0);
});

test("native hook installation pins the existing interpreter, gives cold starts time and diagnoses missing runtime", async (t) => {
  const f = await fixture(t), context = { config: f.config, scope: "user" }, runtime = path.join(f.home, "runtime");
  await fs.mkdir(path.join(runtime, "dist"), { recursive: true, mode: 0o700 });
  await fs.writeFile(path.join(runtime, "dist/hook.js"), "// fixture", { mode: 0o600 });
  const root = await plugin.materializePlugin(context, runtime);
  const hooks = JSON.parse(await fs.readFile(path.join(root, "hooks/hooks.json"), "utf8"));
  for (const entries of Object.values(hooks.hooks)) {
    assert.equal(entries[0].hooks[0].timeout, 10);
    assert.ok(entries[0].hooks[0].command.includes(process.execPath));
  }
  await fs.unlink(path.join(runtime, "dist/hook.js"));
  const inspection = await plugin.grokAdapter.inspect(context);
  assert.equal(inspection.registered, false);
  assert.equal(inspection.detail, "RUNTIME_MISSING_OR_CHANGED");
});

for (const ordering of ['child-first','overlap']) test(`child prompt association is race-safe (${ordering})`, async t => {
  const f=await fixture(t);
  const transcript = async (sid,pid) => {
    const file=path.join(f.home,'.grok/sessions/project',sid,'updates.jsonl');
    await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});
    const updates=[{sessionUpdate:'agent_message_chunk',content:{text:'completed'}},
      {sessionUpdate:'turn_completed',prompt_id:pid,stop_reason:'end_turn',usage:{inputTokens:40,outputTokens:10}}];
    await fs.writeFile(file,updates.map((update,i)=>JSON.stringify({params:{sessionId:sid,update,_meta:{promptId:pid,eventId:sid+'-'+i,streamStartMs:1200,agentTimestampMs:1300}}})).join('\n')+'\n',{mode:0o600});
    return file;
  };
  // Do not expose the completion until after the hooks have raced.
  await capture(event('UserPromptSubmit'),f.config);
  const child=event('UserPromptSubmit',{sessionId:'child',promptId:'child-prompt',subagentType:'worker',time:1100});
  const association=event('SubagentStart',{subagentId:'child',time:1150});
  if(ordering==='child-first') { await capture(child,f.config); await capture(association,f.config); }
  else await Promise.all([capture(child,f.config),capture(association,f.config)]);
  await capture(event('Stop',{sessionId:'child',promptId:'child-prompt',time:1400,transcriptPath:await transcript('child','child-prompt')}),f.config);
  await capture(event('Stop',{time:1500,transcriptPath:await transcript('session','prompt')}),f.config);
  const all=f.spans(), sub=all.find(s=>s.name==='subagent');
  assert.ok(sub); assert.equal(attrs(sub)['fi.agent.outcome'],'success');
  assert.equal(all.filter(s=>attrs(s)['fi.span.kind']==='LLM').length,2);
  assert.equal(all.filter(s=>!s.parentSpanId).length,1);
});
test('saturated completed turn releases its anchors before an ordinary resumed prompt',async t=>{
 const f=await fixture(t), file=path.join(f.home,'.grok/sessions/project/session/updates.jsonl');
 await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700}); await fs.writeFile(file,'',{mode:0o600});
 const submit=async(pid,n,start)=>{
  await capture(event('UserPromptSubmit',{promptId:pid,time:start,transcriptPath:file}),f.config);
  for(let i=0;i<n;i++) {
   await capture(event('PreToolUse',{promptId:pid,time:start+i*2+1,toolId:'tool-'+pid+'-'+i,toolName:'read'}),f.config);
   await capture(event('PostToolUse',{promptId:pid,time:start+i*2+2,toolId:'tool-'+pid+'-'+i,toolName:'read'}),f.config);
  }
  const update={params:{sessionId:'session',update:{sessionUpdate:'turn_completed',prompt_id:pid,stop_reason:'end_turn',usage:{inputTokens:80,outputTokens:20}},_meta:{promptId:pid,agentTimestampMs:start+500}}};
  await fs.appendFile(file,JSON.stringify(update)+'\n');
  await capture(event('Stop',{promptId:pid,time:start+500,transcriptPath:file}),f.config);
 };
 await submit('first',84,1000); const prior=f.spans().length;
 await submit('resumed',1,10000);
 const all=f.spans().slice(prior), root=all.find(s=>!s.parentSpanId);
 assert.ok(root);assert.equal(attrs(root)['fi.agent.outcome'],'success');
 assert.equal(all.filter(s=>attrs(s)['fi.span.kind']==='TOOL').length,1);
 assert.equal(Number(attrs(root)['gen_ai.usage.input_tokens']),80);
 assert.equal(Number(attrs(root)['gen_ai.usage.output_tokens']),20);
});
