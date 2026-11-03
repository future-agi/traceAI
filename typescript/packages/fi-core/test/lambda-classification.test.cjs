const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');
const packageJson = process.env.FI_CORE_PACKAGE_JSON;
if (!packageJson) throw new Error('FI_CORE_PACKAGE_JSON must point at a packed test fixture');
const load = createRequire(packageJson);
const fixture = path.resolve(path.dirname(packageJson), '../../..');
const directory = fs.mkdtempSync(path.join(fixture, 'lambda-classification-'));
// Real exported CJS/ESM handlers must select the package's require/import entries.
// A source-only assertion would miss TypeScript downleveling async functions.
const source = `
import { traceLambdaHandler, type LambdaInvocationContext, type LambdaCallback } from '@traceai/fi-core';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { SimpleSpanProcessor, InMemorySpanExporter } from '@opentelemetry/sdk-trace-base';
type Event = { scenario: string };
export function makeHandlers(telemetry: boolean) {
  const provider = telemetry ? new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new InMemorySpanExporter())] }) : undefined;
  provider?.register();
  const asyncOriginal = async function application(event: Event, _context: LambdaInvocationContext) {
    if (event.scenario === 'pending') await new Promise(() => {});
    await Promise.resolve();
    if (event.scenario === 'error') throw new TypeError('synthetic failure');
    return { statusCode: 200 };
  };
  const callbackOriginal = function application(event: Event, _context: LambdaInvocationContext, callback: LambdaCallback<{ statusCode: number }>) {
    if (event.scenario === 'pending') return;
    callback(event.scenario === 'error' ? new TypeError('synthetic failure') : null, { statusCode: 200 });
  };
  const asyncWrapped = traceLambdaHandler(asyncOriginal, { provider });
  return {
    asyncOriginal, asyncWrapped,
    asyncTwice: traceLambdaHandler(asyncWrapped, { provider }),
    callbackOriginal, callbackWrapped: traceLambdaHandler(callbackOriginal, { provider, completion: 'callback' }),
  };
}
`;
for (const extension of ['cts', 'mts']) fs.writeFileSync(path.join(directory, `handler.${extension}`), source);
const compiled = spawnSync(process.execPath, [load.resolve('typescript/bin/tsc'), '--module', 'NodeNext', '--moduleResolution', 'NodeNext', '--target', 'ES2017', '--strict', '--skipLibCheck', '--types', 'node', '--outDir', path.join(directory, 'out'), ...['cts', 'mts'].map(extension => path.join(directory, `handler.${extension}`))], { cwd: fixture, encoding: 'utf8' });
assert.equal(compiled.status, 0, compiled.stdout + compiled.stderr);
function run(format, telemetry, kind, scenario = '') {
  const child = spawnSync(process.execPath, [path.join(__dirname, 'lambda-classification-client.cjs'), path.join(directory, `out/handler.${format}`), telemetry, kind, scenario], { env: process.env, encoding: 'utf8', timeout: 10000 });
  assert.equal(child.error, undefined, child.error?.message);
  assert.equal(child.status, 0, child.stdout + child.stderr);
  assert.equal(child.stderr, '');
  return JSON.parse(child.stdout);
}
for (const format of ['cjs', 'mjs']) for (const telemetry of ['enabled', 'disabled']) {
  test(`packed AWS handler classification: ${format}, ${telemetry}`, () => {
    const metadata = run(format, telemetry, 'metadata');
    assert.equal(metadata.asyncOriginal.isAsync, true);
    assert.equal(metadata.asyncWrapped.isAsync, true, 'promise wrapper must retain native AsyncFunction classification');
    assert.deepEqual(metadata.asyncTwice, metadata.asyncWrapped);
    assert.equal(metadata.asyncWrapped.argsNum, 2);
    assert.equal(metadata.callbackOriginal.isAsync, false);
    assert.equal(metadata.callbackWrapped.isAsync, false, 'callback wrapper must remain synchronous');
    assert.equal(metadata.callbackWrapped.argsNum, 3);
  });
  // This pinned official runtime is the Node 22 implementation, not evidence of
  // the Node 24/26 runtime contract. Classification above covers all build legs.
  for (const completion of ['async', 'callback']) for (const scenario of ['pending', 'success', 'error']) {
    test(`official Node 22 runtime completion: ${format}, ${telemetry}, ${completion}, ${scenario}`, { skip: Number(process.versions.node.split('.')[0]) !== 22 }, () => {
      const original = run(format, telemetry, completion + 'Original', scenario);
      const wrapped = run(format, telemetry, completion + 'Wrapped', scenario);
      assert.deepEqual(wrapped, original, 'wrapping must not turn an early async exit into a successful null response');
      assert.equal(wrapped.metadata.isAsync, completion === 'async');
      if (scenario === 'pending') {
        assert.equal(wrapped.exitEvents, 1, 'the real process beforeExit event must drive completion');
        if (completion === 'async') {
          assert.equal(wrapped.outcome, 'error');
          assert.equal(wrapped.errorType, 'Runtime.NodeJsExit');
        } else {
          assert.equal(wrapped.outcome, 'success');
          assert.equal(wrapped.result, null);
        }
      } else if (scenario === 'error') {
        assert.equal(wrapped.outcome, 'error');
        assert.equal(wrapped.errorType, 'TypeError');
      } else {
        assert.equal(wrapped.outcome, 'success');
        assert.deepEqual(wrapped.result, { statusCode: 200 });
      }
    });
  }
}
