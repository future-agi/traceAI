// Isolate each invocation so beforeExit fires naturally for an unsettled promise.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');
const { createHash } = require('node:crypto');
const [handlerFile, telemetry, kind, scenario] = process.argv.slice(2);
for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
const fixtures = path.join(__dirname, 'fixtures/aws-runtime');
const provenance = JSON.parse(fs.readFileSync(path.join(fixtures, 'provenance.json'), 'utf8'));
for (const [name, record] of Object.entries(provenance.files)) {
  assert.equal(createHash('sha256').update(fs.readFileSync(path.join(fixtures, name))).digest('hex'), record.sha256);
}
const noop = () => {};
const logging = { structuredConsole: { logError: noop } };
const verbose = { logger: () => ({ verbose: noop, vverbose: noop }) };
const beforeExit = require('./fixtures/aws-runtime/BeforeExitListener.js');
const errors = require('./fixtures/aws-runtime/Errors.js');
// Evaluate the pinned sources unchanged. Only runtime I/O, diagnostics and unused
// streaming plumbing are stubbed; classification and completion logic are real.
function officialModule(name, imports) {
  const module = { exports: {} };
  const source = fs.readFileSync(path.join(fixtures, name), 'utf8');
  vm.runInThisContext('(function(require,module,exports){' + source + '\n})', { filename: `AWS-${name}` })(dependency => {
    if (Object.hasOwn(imports, dependency)) return imports[dependency];
    throw new Error(`Unexpected runtime dependency: ${dependency}`);
  }, module, module.exports);
  return module.exports;
}
const userFunction = officialModule('UserFunction.js', {
  path, fs, './Errors.js': errors, './VerboseLog.js': verbose, './HttpResponseStream': {},
});
const callbackContext = officialModule('CallbackContext.js', {
  './BeforeExitListener.js': beforeExit, './LogPatch': logging,
});
class InvokeContext {
  constructor() { this.invokeId = 'synthetic-early-exit'; }
  updateLoggingContext() {}
  attachEnvironmentData(context) {
    context.callbackWaitsForEmptyEventLoop = false;
    return Object.assign(context, { awsRequestId: this.invokeId, getRemainingTimeInMillis: () => 10000 });
  }
}
const Runtime = officialModule('Runtime.js', {
  './InvokeContext.js': InvokeContext, './CallbackContext.js': callbackContext,
  './StreamingContext.js': {}, './BeforeExitListener.js': beforeExit,
  './UserFunction.js': userFunction, './Errors.js': errors,
  './VerboseLog.js': verbose, './LogPatch': logging,
});
(async () => {
  const module = await import(pathToFileURL(handlerFile).href);
  const handlers = module.makeHandlers(telemetry === 'enabled');
  if (kind === 'metadata') {
    console.log(JSON.stringify(Object.fromEntries(Object.entries(handlers).map(([name, handler]) => [name, userFunction.getHandlerMetadata(handler)]))));
    return;
  }
  const handler = handlers[kind];
  assert.equal(typeof handler, 'function');
  const metadata = userFunction.getHandlerMetadata(handler);
  let exitEvents = 0, posts = 0;
  function report(outcome) {
    assert.equal(++posts, 1, 'an invocation must report its outcome only once');
    beforeExit.reset();
    console.log(JSON.stringify({ metadata, exitEvents, ...outcome }));
  }
  const client = {
    nextInvocation: async () => ({ bodyJson: JSON.stringify({ scenario }), headers: {} }),
    postInvocationResponse: result => report({ outcome: 'success', result }),
    postInvocationError: error => report({ outcome: 'error', errorType: error.name }),
  };
  const runtime = new Runtime(client, handler, metadata, {});
  // One invocation only, without polling a live Lambda service afterwards.
  runtime.scheduleIteration = noop;
  process.on('beforeExit', () => { exitEvents++; beforeExit.invoke(); });
  await runtime.handleOnceNonStreaming();
})().catch(error => { console.error(error); process.exitCode = 1; });
