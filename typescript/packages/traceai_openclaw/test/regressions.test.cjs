const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const http = require('node:http');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { pathToFileURL } = require('node:url');
const exec = promisify(execFile);
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { normalizeSpan } = require('../dist/normalize.js');
const { withBootstrap, restoreNodeOptions } = require('../dist/node-options.js');
const { createOpenClawAdapter } = require('../dist/adapter.js');
const { setup, inspectStatus } = require('@traceai/agent-core');
const config = {
  captureContent: true, hideInputs: false, hideOutputs: false, hideErrorDetails: false,
  credentials: { apiKey: 'SYNTHETIC_API_' + 'A'.repeat(48), secretKey: 'SYNTHETIC_SECRET_' + 'B'.repeat(48) },
  projectName: 'test',
};
function span(name, attributes = {}) {
  return { name, attributes, status: { code: 2 }, events: [], links: [],
    resource: resourceFromAttributes({}), spanContext: () => ({traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1}) };
}

test('ordinary Node options survive adding and removing only our bootstrap', () => {
  const original = '--max-old-space-size=4096 --use-system-ca --use-openssl-ca --title="a b"';
  const env = { NODE_OPTIONS: withBootstrap(original, 'file:///tmp/bootstrap.mjs'), FI_OPENCLAW_NODE_OPTIONS: original };
  assert.equal(env.NODE_OPTIONS, original + ' --import=file:///tmp/bootstrap.mjs');
  restoreNodeOptions(env);
  assert.deepEqual(env, { NODE_OPTIONS: original });
  const empty = { NODE_OPTIONS: withBootstrap(undefined, 'file:///tmp/bootstrap.mjs'), FI_OPENCLAW_NODE_OPTIONS: '' };
  restoreNodeOptions(empty);
  assert.deepEqual(empty, {});
  assert.doesNotThrow(() => withBootstrap('--title=--require', 'file:///tmp/bootstrap.mjs'));
});

test('real bootstrap removes itself but preserves runtime options in tool subprocesses', async (t) => {
  const home=await fs.mkdtemp(join(tmpdir(),'fi-openclaw-preload-'));
  t.after(()=>fs.rm(home,{recursive:true,force:true}));
  const original='--max-old-space-size=4096 --use-openssl-ca';
  const env={...process.env,HOME:home,FI_AGENT_CONFIG_DIR:join(home,'config'),OPENCLAW_STATE_DIR:join(home,'host'),
    FI_AGENT_ENABLED:'false',FI_OPENCLAW_BOOTSTRAP:'1',FI_OPENCLAW_NODE_OPTIONS:original,
    FI_OPENCLAW_BRIDGE_KEY:'a'.repeat(64), FI_OPENCLAW_BRIDGE_FD:'3',
    FI_API_KEY:'synthetic-public',FI_SECRET_KEY:'synthetic-private',
    NODE_OPTIONS:withBootstrap(original,pathToFileURL(join(__dirname,'../native/bootstrap.mjs')).href)};
  const childCode='process.stdout.write(JSON.stringify({options:process.env.NODE_OPTIONS, bootstrap:process.env.FI_OPENCLAW_BOOTSTRAP, api:process.env.FI_API_KEY, secret:process.env.FI_SECRET_KEY, bridgeKey:process.env.FI_OPENCLAW_BRIDGE_KEY, bridgeFd:process.env.FI_OPENCLAW_BRIDGE_FD}))';
  const code=`const {execFileSync}=require('node:child_process');process.stdout.write(execFileSync(process.execPath,['-e',${JSON.stringify(childCode)}]));`;
  const result=await exec(process.execPath,['-e',code],{env});
  assert.deepEqual(JSON.parse(result.stdout),{options:original});
});

test('genuine import, require, and loader options are rejected without erasing them', () => {
  for (const options of ['--import=a', '--import a', '"--import=a"', '--require=a', '--require a', '-r a', '-ra', '--loader=a', '--experimental-loader a', '--experimental_loader=a', '--max-old-space-size=4096 --require a'])
    assert.throws(() => withBootstrap(options, 'file:///tmp/bootstrap.mjs'), /OPENCLAW_EXISTING_PRELOAD/, options);
  assert.throws(() => withBootstrap('--title="unclosed', 'file:///tmp/bootstrap.mjs'), /INVALID_NODE_OPTIONS/);
});

test('known credentials straddling capture limits never leak prefixes in input, output, tools or status', () => {
  for (const secret of Object.values(config.credentials)) {
    for (const start of [19965, 19999, 20000 - secret.length, 20000 - secret.length + 1]) {
      const value = 'x'.repeat(start) + secret + ' tail';
      const source = span('openclaw.model.call', {
        'openclaw.content.input_messages': value,
        'openclaw.content.output_messages': value,
      });
      source.status.message = value;
      const result = normalizeSpan(source, config);
      const tool = normalizeSpan(span('openclaw.tool.execution', {
        'openclaw.content.tool_input': value, 'openclaw.content.tool_output': value,
      }), config);
      for (const text of [result.attributes['input.value'], result.attributes['output.value'], result.status.message,
        tool.attributes['input.value'], tool.attributes['output.value']]) {
        assert.ok(text.length <= 20000);
        assert.ok(!text.includes(secret.slice(0, Math.min(10, 20000 - start))));
        assert.ok(text.endsWith('[REDACTED]'.slice(0, 20000 - start)) || text.includes('[REDACTED]'));
      }
    }
  }
  // Earlier redaction must not expose a second key truncated at the lookahead boundary.
  const value = config.credentials.apiKey.repeat(100) + 'x'.repeat(20000) + config.credentials.secretKey;
  assert.ok(!JSON.stringify(normalizeSpan(span('openclaw.model.call', {'openclaw.content.input_messages':value}),config)).includes('SYNTHETIC'));
});

test('experimental semantic conventions preserve requests and opaque turns, not unknown families', () => {
  for (const operation of ['chat', 'text_completion', 'generate_content']) {
    const source = span(operation + ' test-model', {
      'gen_ai.operation.name': operation, 'openclaw.model_call.observation_unit': 'request',
      'gen_ai.usage.input_tokens': 11, 'gen_ai.usage.output_tokens': 7,
      'gen_ai.provider.name': 'mock', 'openclaw.content.input_messages': 'prompt',
      'openclaw.content.output_messages': 'answer', 'arbitrary.payload': 'private',
    });
    const output = normalizeSpan(source, config);
    assert.equal(output.name, 'openclaw.model.call');
    assert.equal(output.attributes['fi.span.kind'], 'LLM');
    assert.equal(output.attributes['gen_ai.usage.total_tokens'], 18);
    assert.equal(output.attributes['input.value'], 'prompt');
    assert.equal(output.attributes['output.value'], 'answer');
    assert.equal(output.attributes['arbitrary.payload'], undefined);
    assert.equal(normalizeSpan({...source, attributes:{...source.attributes, 'openclaw.model_call.observation_unit':undefined}}, config), undefined);
    for (const changed of [
      {...source, attributes:{...source.attributes, 'gen_ai.operation.name':'other'}},
      {...source, name:'chat private content'},
      {...source, attributes:{...source.attributes, 'openclaw.model_call.observation_unit':'new-unit'}},
    ]) {
      const gap=normalizeSpan(changed,config);
      assert.equal(gap.name,'openclaw.model.call');
      assert.equal(gap.attributes['fi.agent.gap'],'UNSUPPORTED_NATIVE_SPAN_SHAPE');
      assert.equal(gap.attributes['input.value'],undefined);
      assert.equal(gap.attributes['gen_ai.usage.input_tokens'],undefined);
    }
  }
  const turn = normalizeSpan(span('invoke_agent', {
    'gen_ai.operation.name':'invoke_agent', 'openclaw.model_call.observation_unit':'turn', 'gen_ai.usage.input_tokens':99,
  }), config);
  assert.equal(turn.attributes['fi.span.kind'], 'AGENT');
  assert.equal(turn.attributes['gen_ai.usage.input_tokens'], 99);
  assert.equal(turn.attributes['fi.agent.usage_owner'], 'native-turn');
  assert.equal(normalizeSpan(span('invoke_agent', {'gen_ai.operation.name':'invoke_agent','openclaw.model_call.observation_unit':'request'}),config).attributes['fi.agent.gap'],'UNSUPPORTED_NATIVE_SPAN_SHAPE');
});

test('setup and status describe existing exporter conflicts without disclosing host errors', async (t) => {
  const home = await fs.mkdtemp(join(tmpdir(), 'fi-openclaw-conflict-'));
  const server = http.createServer((req, res) => { req.resume(); res.setHeader('content-type','application/json'); res.end('{}'); });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await fs.rm(home,{recursive:true,force:true}); });
  const host = join(home, '.openclaw'); await fs.mkdir(host,{mode:0o700});
  const source = '{"diagnostics":{"otel":{"enabled":true,"endpoint":"https://existing.invalid"}}}';
  await fs.writeFile(join(host,'openclaw.json'),source,{mode:0o600});
  const adapter = createOpenClawAdapter(async args => args[0]==='--version' ? 'OpenClaw 2026.10.5' : '{}');
  const options = { agent:'openclaw', home, env:{}, adapter, credentials:config.credentials,
    otlpEndpoint:`http://127.0.0.1:${server.address().port}/tracer/v1/traces` };
  const result = await setup(options);
  assert.equal(result.code,'EXISTING_OPENCLAW_EXPORTER');
  assert.equal(result.registered,false);
  assert.match(result.detail,/Keep your existing direct exporter/);
  assert.equal(await fs.readFile(join(host,'openclaw.json'),'utf8'),source);
  const status = await inspectStatus(options);
  assert.equal(status.detail,'EXISTING_OPENCLAW_EXPORTER');
  assert.equal(status.diagnostics.find(d=>d.code==='EXISTING_OPENCLAW_EXPORTER').action,result.detail);
  assert.ok(status.issues.includes('DUPLICATE_REGISTRATION'));
  assert.ok(!status.detail.includes('No direct exporter'));
  for (const message of ['raw sensitive host error', 'EXISTING_OPENCLAW_EXPORTER raw host error', 'toString']) {
    const failure = await setup({...options, adapter:{...adapter, install:async()=>{throw new Error(message);}}});
    assert.equal(failure.code,'INSTALLATION_FAILED');
    assert.equal(failure.detail,undefined);
  }
});

test('release metadata updates both adapter and bundled native plugin versions', {skip: process.env.FI_TEST_ADAPTER_ONLY === '1' && 'shared release metadata is owned by the integration assembler'}, async () => {
  const root = join(__dirname, '../../../..');
  const pkg = require('../package.json');
  const native = require('../native/package.json');
  const release = require(join(root,'release-please-config.json')).packages['typescript/packages/traceai_openclaw'];
  assert.equal(native.version,pkg.version);
  assert.equal(createOpenClawAdapter().version,pkg.version);
  assert.equal(require(join(root,'.release-please-manifest.json'))['typescript/packages/traceai_openclaw'],pkg.version);
  assert.equal(release['package-name'],pkg.name);
  assert.ok(release['extra-files'].includes('src/adapter.ts'));
  assert.ok(release['extra-files'].some(entry=>entry.path==='native/package.json'&&entry.type==='json'&&entry.jsonpath==='$.version'));
  assert.match(await fs.readFile(join(__dirname,'../src/adapter.ts'),'utf8'),/const VERSION = .*x-release-please-version/);
});


test('2026 host updates retain registration; unknown major versions remain explicit failures', async t => {
  const home=await fs.mkdtemp(join(tmpdir(),'fi-openclaw-version-'));
  t.after(()=>fs.rm(home,{recursive:true,force:true}));
  const host=join(home,'.openclaw'); await fs.mkdir(host,{mode:0o700});
  await fs.writeFile(join(host,'openclaw.json'),'{}',{mode:0o600});
  const config=await require('@traceai/agent-core').resolveConfig({agent:'openclaw',home,env:{}});
  let version='2026.10.5';
  const adapter=createOpenClawAdapter(async args=>args[0]==='--version' ? `OpenClaw ${version}` : JSON.stringify({plugin:{
    enabled:true,status:'loaded',trustedOfficialInstall:true,
    packageName:args[2]==='diagnostics-otel'?'@openclaw/diagnostics-otel':'@traceai/futureagi-openclaw',
    version:args[2]==='diagnostics-otel'?version:require('../package.json').version,
  }}));
  const ctx={config,scope:'user'};
  await adapter.install(ctx);
  for (version of ['2026.10.6','2026.11.5']) {
    assert.equal((await adapter.detect(ctx)).supported,true);
    assert.equal((await adapter.inspect(ctx)).registered,true);
  }
  for (version of ['2027.1.1','2026.9.30','2026.10.4']) {
    assert.equal((await adapter.detect(ctx)).supported,false);
    assert.equal((await adapter.inspect(ctx)).registered,false);
  }
});

test('unknown observation units keep a structural gap; tool names use backend semantics', () => {
  const unknown=normalizeSpan(span('openclaw.model.call',{
    'openclaw.model_call.observation_unit':'new-unit','gen_ai.usage.input_tokens':100,
    'openclaw.content.input_messages':'private',
  }),config);
  assert.equal(unknown.attributes['fi.agent.gap'],'UNSUPPORTED_NATIVE_SPAN_SHAPE');
  assert.equal(unknown.attributes['fi.span.kind'],'AGENT');
  assert.equal(unknown.attributes['gen_ai.usage.input_tokens'],undefined);
  assert.equal(unknown.attributes['input.value'],undefined);
  const tool=normalizeSpan(span('openclaw.tool.execution',{
    'openclaw.toolName':'read','gen_ai.tool.call.id':'tool-1',
  }),config);
  assert.equal(tool.attributes['tool.name'],'read');
  assert.equal(tool.attributes['tool_call.id'],'tool-1');
  assert.equal(tool.attributes['tool.call.id'],undefined);
  const model=normalizeSpan(span('chat provider/model',{
    'gen_ai.operation.name':'chat','openclaw.model_call.observation_unit':'request',
  }),config);
  assert.equal(model.attributes['fi.span.kind'],'LLM');
});
