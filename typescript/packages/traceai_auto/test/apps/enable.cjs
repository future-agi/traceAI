if (process.env.ENABLE_MODE !== 'absent') exports.enable = function(options) {
  const assert = require('node:assert/strict');
  const agent = globalThis[Symbol.for('traceai.auto.agent.v1')];
  assert.equal(arguments.length, 1);
  assert.equal(options.spanProcessor, agent.processor);
  assert.equal(Object.getPrototypeOf(options.tracerProvider), agent.provider);
  globalThis.__traceai_enable_calls = (globalThis.__traceai_enable_calls || 0) + 1;
  if (process.env.ENABLE_MODE === 'throw') throw new Error('broken optional entry');
  return { status: 'instrumented' };
};
