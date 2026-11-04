// Run in a child process so the official runtime's real beforeExit semantics apply.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON);
for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
const { NodeTracerProvider } = load('@opentelemetry/sdk-trace-node');
const { BatchSpanProcessor } = load('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = load('@opentelemetry/exporter-trace-otlp-proto');
const { traceLambdaHandler } = load('@traceai/fi-core');
const fixtures = path.join(__dirname, 'fixtures/aws-runtime');
const provenance = JSON.parse(fs.readFileSync(path.join(fixtures, 'provenance.json'), 'utf8'));
for (const [name, record] of Object.entries(provenance.files)) {
  assert.equal(createHash('sha256').update(fs.readFileSync(path.join(fixtures, name))).digest('hex'), record.sha256);
}
const beforeExit = require('./fixtures/aws-runtime/BeforeExitListener.js');
const callbackModule = { exports: {} };
const source = fs.readFileSync(path.join(fixtures, 'CallbackContext.js'), 'utf8');
// The official callback implementation is untouched; only the logger is stubbed.
vm.runInThisContext('(function(require, module, exports) {' + source + '\n})', { filename: 'AWS-CallbackContext.js' })(name => {
  if (name === './BeforeExitListener.js') return beforeExit;
  if (name === './LogPatch') return { structuredConsole: { logError() {} } };
  throw new Error(`Unexpected runtime import: ${name}`);
}, callbackModule, callbackModule.exports);

const [url, mode, outcome] = process.argv.slice(2);
const result = { statusCode: 200, body: 'Ready' };
const failure = new TypeError('PRIVATE_CALLBACK_ERROR');
const provider = new NodeTracerProvider({ spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({
  url, headers: { 'x-api-key': 'synthetic-api', 'x-secret-key': 'synthetic-secret' }, timeoutMillis: 800,
}), { scheduledDelayMillis: 60000 })] });
provider.register();
const started = performance.now();
let callbackMs, callbackCalls = 0, flagWrites = 0;
let applicationWorkFinished = false;
const finish = (error, value) => {
  const postMs = performance.now() - started;
  assert.equal(error, outcome === 'success' ? null : failure);
  if (outcome === 'success') assert.deepEqual(value, result);
  assert.equal(callbackCalls, 1);
  assert.equal(invocation.callbackWaitsForEmptyEventLoop, mode !== 'false');
  assert.equal(flagWrites, 0, 'wrapper must not change the application-owned flag');
  assert.ok(postMs < 500, `runtime completion exceeded the wait budget: ${postMs}ms`);
  if (mode !== 'false') assert.equal(applicationWorkFinished, true, 'retain application event-loop waiting semantics');
  console.log(JSON.stringify({ mode, outcome, callbackMs, postMs, callbackCalls, flagWrites }));
  beforeExit.reset();
  // Record the actual runtime response boundary, not process exit. Let the pending
  // exporter finish its transport timeout so the parent can inspect the wire even
  // if CPU contention delayed opening the socket until after the 20ms wait.
  process.exitCode = 0;
};
const [callback, invocation] = callbackModule.exports.build({
  postInvocationResponse: value => finish(null, value),
  postInvocationError: error => finish(error),
}, 'synthetic-id', () => {});
Object.assign(invocation, {
  awsRequestId: 'synthetic-id', functionName: 'orders',
  // Fix the budget at the wrapper's remaining-time read. CPU contention during
  // fixture setup must not turn this stalled-export test into an exhausted-budget
  // test (covered separately by lambda-lifecycle.test.cjs).
  getRemainingTimeInMillis: () => 100,
});
if (mode === 'false') invocation.callbackWaitsForEmptyEventLoop = false;
if (mode === 'true') invocation.callbackWaitsForEmptyEventLoop = true;
const flag = Object.getOwnPropertyDescriptor(invocation, 'callbackWaitsForEmptyEventLoop');
Object.defineProperty(invocation, 'callbackWaitsForEmptyEventLoop', {
  get: flag.get, set(value) { flagWrites++; flag.set.call(invocation, value); },
});
process.on('beforeExit', beforeExit.invoke);
const handler = traceLambdaHandler((_event, _ctx, cb) => {
  if (mode !== 'false') setTimeout(() => { applicationWorkFinished = true; }, 60);
  if (outcome === 'throw') throw failure;
  cb(outcome === 'error' ? failure : null, result);
}, { provider, completion: 'callback', maxFlushWaitMillis: 20, remainingTimeReserveMillis: 50 });
handler({}, invocation, (error, value) => {
  callbackCalls++; callbackMs = performance.now() - started; callback(error, value);
});
