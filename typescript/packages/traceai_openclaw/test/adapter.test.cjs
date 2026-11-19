const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtemp, mkdir, writeFile, readFile, chmod, symlink } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { normalizeSpan } = require('../dist/normalize.js');
const { localArguments, overlay, parseHost, createOpenClawAdapter } = require('../dist/adapter.js');
const { resolveConfig } = require('@traceai/agent-core');
const config = {
  captureContent: true, hideInputs: false, hideOutputs: false, hideErrorDetails: false,
  credentials: { apiKey: 'dummy-public', secretKey: 'dummy-private' }, projectName: 'demo', userId: 'opaque-user',
};
function span(name, attrs = {}) {
  return {
    name, attributes: attrs, resource: resourceFromAttributes({'host.name': 'private-host'}),
    spanContext: () => ({traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1}),
    parentSpanContext: {traceId: 'a'.repeat(32), spanId: 'c'.repeat(16), traceFlags: 1},
    status: { code: 2, message: 'secret=dummy-private' }, events: [{name: 'private event'}], links: [],
  };
}
test('suppresses native usage rollups instead of double counting', () => {
  assert.equal(normalizeSpan(span('openclaw.model.usage', {'gen_ai.usage.input_tokens': 11}), config), undefined);
  assert.equal(normalizeSpan(span('openclaw.customer.secret'), config), undefined);
  const s = normalizeSpan(span('openclaw.model.call', {'gen_ai.usage.input_tokens': 11, 'gen_ai.usage.output_tokens': 7}), config);
  assert.equal(s.attributes['gen_ai.usage.total_tokens'], 18);
  assert.equal(s.attributes['fi.span.kind'], 'LLM');
  assert.equal(s.attributes['fi.agent.turn_correlation'], 'unverified');
});
test('retains native ids and counts reported opaque-turn usage once without inventing generations', () => {
  const source = span('openclaw.model.call', {'openclaw.model_call.observation_unit': 'turn', 'gen_ai.usage.input_tokens': 11});
  const result = normalizeSpan(source, config, 'opaque-session');
  assert.deepEqual(result.spanContext(), source.spanContext());
  assert.deepEqual(result.parentSpanContext, source.parentSpanContext);
  assert.equal(result.attributes['session.id'], 'opaque-session');
  assert.equal(result.attributes['gen_ai.usage.input_tokens'], 11);
  assert.equal(result.attributes['fi.agent.usage_owner'], 'native-turn');
  assert.equal(result.attributes['fi.span.kind'], 'AGENT');
});
test('privacy reconstructs resources and drops event/unknown backdoors', () => {
  const attrs = {'openclaw.content.input_messages': 'dummy-private ' + 'x'.repeat(30000), 'arbitrary.payload': 'leak', 'gen_ai.input.messages': 'leak'};
  const result = normalizeSpan(span('openclaw.model.call', attrs), config);
  assert.ok(result.attributes['input.value'].length <= 20000);
  assert.ok(!result.attributes['input.value'].includes('dummy-private'));
  assert.equal(result.attributes['arbitrary.payload'], undefined);
  assert.equal(result.resource.attributes['host.name'], undefined);
  assert.equal(result.resource.attributes.project_type, 'observe');
  assert.deepEqual(result.events, []);
  const hidden = normalizeSpan(span('openclaw.model.call', attrs), {...config, captureContent:false, hideInputs:true, hideOutputs:true, hideErrorDetails:true});
  assert.equal(hidden.attributes['input.value'], undefined);
  assert.equal(hidden.status.message, undefined);
  const failure = normalizeSpan(span('openclaw.tool.execution', {'openclaw.content.tool_output':'private failure'}), {...config, hideErrorDetails:true});
  assert.equal(failure.attributes['output.value'],undefined);
});
test('launcher refuses gateway/exec and preserves explicit session ids', () => {
  assert.throws(() => localArguments(['agent', 'exec', '--local']), /LOCAL_AGENT_ONLY/);
  assert.throws(() => localArguments(['gateway']), /LOCAL_AGENT_ONLY/);
  assert.throws(() => localArguments(['agent', '--local', '--session-key', 'other']), /UNSUPPORTED_ROUTING/);
  assert.equal(localArguments(['agent', '--local', '--session-id', 'session']).session, 'session');
  assert.ok(localArguments(['agent', '--local']).session);
});
test('overlay keeps host settings without copying native destinations or enabling logs/metrics', () => {
  const original = { plugins:{allow:['other','diagnostics-otel']}, models:{mode:'merge'}, diagnostics:{otel:{enabled:false, headers:{other:'dummy'}}}};
  const result = overlay(original, false, true);
  assert.deepEqual(result.plugins, original.plugins);
  assert.deepEqual(result.models, original.models);
  assert.equal(result.diagnostics.otel.metrics, false);
  assert.equal(result.diagnostics.otel.logs, false);
  assert.equal(result.diagnostics.otel.captureContent, false);
  assert.equal(result.diagnostics.otel.headers, undefined);
});
test('installation merges JSON5 comments, never overwrites allowlists, refuses edited ownership, and restores owned fields', async () => {
  const home = await mkdtemp(join(tmpdir(), 'fi-openclaw-unit-'));
  const host = join(home,'.openclaw'); await mkdir(host,{mode:0o700});
  const path = join(host, 'openclaw.json');
  await writeFile(path, '// customer comment\n{plugins:{allow:["other","diagnostics-otel","futureagi-openclaw"]},custom:42}\n',{mode:0o600});
  await chmod(path,0o644); // host-owned non-secret JSON5 may predate the private writer
  const cfg = await resolveConfig({agent:'openclaw',home,env:{}});
  const adapter = createOpenClawAdapter(async (args) => {
    if(args[0]==='--version') return 'OpenClaw 2026.10.5 (abc)';
    assert.equal(args[1],'inspect');
    return JSON.stringify({plugin:{enabled:true,status:'loaded',trustedOfficialInstall:args[2]==='diagnostics-otel',packageName:args[2]==='diagnostics-otel'?'@openclaw/diagnostics-otel':'@traceai/futureagi-openclaw',version:args[2]==='diagnostics-otel'?'2026.10.5':require('../package.json').version}});
  });
  const ctx = {config:cfg,scope:'user'};
  assert.equal((await adapter.install(ctx)).registered, true);
  await adapter.install(ctx);
  const text = await readFile(path,'utf8');
  assert.match(text,/customer comment/); assert.equal(parseHost(text).custom,42);
  assert.deepEqual(parseHost(text).plugins.allow,['other','diagnostics-otel','futureagi-openclaw']);
  assert.equal(parseHost(text).diagnostics.otel.enabled,false);
  await adapter.uninstall(ctx);
  assert.equal(parseHost(await readFile(path,'utf8')).diagnostics,undefined);
  await adapter.install(ctx);
  await writeFile(path, JSON.stringify({...parseHost(await readFile(path,'utf8')),diagnostics:{otel:{enabled:true}}}));
  await assert.rejects(adapter.uninstall(ctx),/REGISTRATION_MODIFIED/);
});
test('a plugin with the official id but no official provenance is not accepted', async () => {
  const home = await mkdtemp(join(tmpdir(), 'fi-openclaw-trust-'));
  const host=join(home,'.openclaw');await mkdir(host,{mode:0o700});
  await writeFile(join(host,'openclaw.json'),'{}',{mode:0o600});
  const cfg=await resolveConfig({agent:'openclaw',home,env:{}});
  const adapter=createOpenClawAdapter(async()=>JSON.stringify({plugin:{enabled:true,status:'loaded',version:'2026.10.5'}}));
  await assert.rejects(adapter.install({config:cfg,scope:'user'}),/PLUGIN_REQUIRES_ATTENTION/);
});
test('host-directory override and symlink protection', async () => {
  const home = await mkdtemp(join(tmpdir(), 'fi-openclaw-path-'));
  const state = join(home,'state'); await mkdir(state,{mode:0o700});
  const cfg = await resolveConfig({agent:'openclaw',home,env:{OPENCLAW_STATE_DIR:state}});
  assert.equal(cfg.paths.user,join(state,'futureagi.json'));
  const target = join(home,'target'); await writeFile(target,'{}',{mode:0o600});
  await symlink(target,join(state,'openclaw.json'));
  const adapter = createOpenClawAdapter(async()=>JSON.stringify({plugin:{enabled:true,version:'2026.10.5'}}));
  await assert.rejects(adapter.install({config:cfg,scope:'user'}));
});
