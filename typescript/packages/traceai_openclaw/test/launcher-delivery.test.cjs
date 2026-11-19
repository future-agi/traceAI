const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {readFileSync, fstatSync, writeSync} = require('node:fs');
const {tmpdir} = require('node:os');
const {join} = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {BasicTracerProvider} = require('@opentelemetry/sdk-trace-base');
const {resolveConfig, readJson, deliveryStatePath} = require('@traceai/agent-core');
const {createLauncherDelivery, LauncherCaptureProcessor} = require('../dist/launcher-delivery.js');

async function fixture(t, delay = 0) {
  const home = await fs.mkdtemp(join(tmpdir(), 'fi-openclaw-delivery-'));
  const received = [];
  const server = http.createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    const payload = JSON.parse(body);
    for (const r of payload.resourceSpans ?? []) for (const s of r.scopeSpans ?? []) received.push(...s.spans);
    if (delay) await new Promise(resolve => setTimeout(resolve, delay));
    if (!res.destroyed) { res.setHeader('content-type', 'application/json'); res.end('{}'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const config = await resolveConfig({agent:'openclaw', home, env:{FI_AGENT_ENABLED:'true',
    FI_API_KEY:'synthetic-public', FI_SECRET_KEY:'synthetic-private', FI_OTLP_ENDPOINT:`http://127.0.0.1:${server.address().port}/traces`}});
  const delivery = await createLauncherDelivery(config);
  t.after(async () => { await delivery.finish(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await fs.rm(home,{recursive:true,force:true}); });
  return {home, config, delivery, received};
}
function emit(capture, count = 1, content = 'PRIVATE_PROMPT') {
  const provider = new BasicTracerProvider({spanProcessors:[capture]});
  for (let i = 0; i < count; i++) provider.getTracer('test').startSpan('openclaw.model.call', {attributes:{
    'openclaw.model_call.observation_unit':'request', 'openclaw.content.input_messages':content,
    'gen_ai.usage.input_tokens':11, 'gen_ai.usage.output_tokens':7,
  }}).end();
}

test('early rejected prompts retain one outcome root with no invented model or usage', async t => {
  for (const exitCode of [0, 1, 130]) {
    const f = await fixture(t);
    await f.delivery.finish(exitCode);
    await f.delivery.finish(exitCode);
    assert.equal(f.received.length, 1);
    const root = f.received[0];
    const attrs = Object.fromEntries(root.attributes.map(a => [a.key, a.value.stringValue ?? a.value.boolValue]));
    assert.equal(root.name, 'openclaw.prompt');
    assert.equal(root.status.code, exitCode === 0 ? 1 : 2);
    assert.equal(attrs['fi.agent.gap'], 'MISSING_NATIVE_SPANS');
    assert.equal(attrs['fi.usage.contribution'], 'none');
    assert.ok(!root.attributes.some(a => a.key.startsWith('gen_ai.usage.')));
  }
});

test('capture is anonymous, encrypted and flushed by the parent, not the host lifecycle', async t => {
  const f = await fixture(t, 50);
  assert.equal(fstatSync(f.delivery.fd).mode & 0o777, 0o600);
  assert.equal(fstatSync(f.delivery.fd).nlink, 0, 'descriptor has no filesystem name');
  const capture = new LauncherCaptureProcessor(f.config, f.delivery.fd, Buffer.from(f.delivery.key,'hex'));
  emit(capture, 3);
  // Test-only direct descriptor read; never a user credential or host data file.
  assert.ok(!readFileSync(f.delivery.fd).includes('PRIVATE_PROMPT'));
  assert.deepEqual(await fs.readdir(f.config.paths.stateDir), []);
  await f.delivery.finish();
  assert.equal(f.received.length, 4);
  assert.equal(f.delivery.processor.state.exported, 4);
  assert.equal(f.delivery.processor.state.dropped, 0);
  assert.equal((await readJson(deliveryStatePath(f.config))).exported,4);
});

test('explicit child process.exit cannot discard completed spans or slow exporter receipts', async t => {
  const f = await fixture(t, 500);
  const captureModule = join(__dirname, '../dist/launcher-delivery.js');
  const providerModule = require.resolve('@opentelemetry/sdk-trace-base');
  const script = `
    const {LauncherCaptureProcessor} = require(${JSON.stringify(captureModule)});
    const {BasicTracerProvider} = require(${JSON.stringify(providerModule)});
    const config = JSON.parse(process.env.FIXTURE_CONFIG);
    const capture = new LauncherCaptureProcessor(config,3,Buffer.from(process.env.FIXTURE_KEY,'hex'));
    const provider = new BasicTracerProvider({spanProcessors:[capture]});
    const tracer = provider.getTracer('test');
    for (const name of ['openclaw.context.assembled','openclaw.model.call','openclaw.run','openclaw.harness.run']) tracer.startSpan(name).end();
    process.exit(7);
  `;
  const child = spawn(process.execPath,['-e',script], {env:{...process.env,FIXTURE_CONFIG:JSON.stringify(f.config),FIXTURE_KEY:f.delivery.key},stdio:['ignore','pipe','pipe',f.delivery.fd]});
  const code = await new Promise((resolve,reject) => {child.on('error',reject);child.on('close',resolve);});
  assert.equal(code,7);
  await f.delivery.finish();
  assert.equal(f.received.length,5);
  assert.equal(f.delivery.processor.state.exported,5);
  assert.equal(f.delivery.processor.state.dropped,0);
});

test('capture budget drops are durable; malformed encrypted frames fail open', async t => {
  const f = await fixture(t);
  const capture = new LauncherCaptureProcessor(f.config,f.delivery.fd,Buffer.from(f.delivery.key,'hex'));
  emit(capture,240,'x'.repeat(20000));
  assert.ok(fstatSync(f.delivery.fd).size <= 4 * 1024 * 1024 + 12);
  await f.delivery.finish();
  assert.ok(f.delivery.processor.state.dropped > 0);
  assert.equal(f.delivery.processor.state.exported + f.delivery.processor.state.dropped,241);
  const receipt=await readJson(deliveryStatePath(f.config));
  assert.equal(receipt.exported + receipt.dropped,241);
  // The serializer adds OTLP framing overhead: a fully buffered burst can hit
  // both the capture budget and the independently bounded export queue.
  assert.ok(['CAPTURE_LIMIT','QUEUE_LIMIT'].includes(receipt.lastError));
  const bad = await fixture(t);
  const writer = new LauncherCaptureProcessor(bad.config,bad.delivery.fd,Buffer.from(bad.delivery.key,'hex'));
  emit(writer);
  writeSync(bad.delivery.fd,Buffer.from([0xff]),0,1,12+4+12);
  await bad.delivery.finish();
  assert.equal(bad.received.length,1, 'malformed native capture still retains the launched prompt root');
  assert.equal(bad.received[0].name, 'openclaw.prompt');
  assert.equal(bad.delivery.processor.state.lastError,'CAPTURE_CHANNEL_INVALID');
});
