if (process.env.ENABLE_MODE !== 'absent') exports.enable = function(options) {
  const assert = require('node:assert/strict');
  const agent = globalThis[Symbol.for('traceai.auto.agent.v1')];
  assert.equal(arguments.length, 1);
  assert.equal(options.spanProcessor, agent.processor);
  // A facade over the agent's selected provider, never that provider itself.
  assert(agent.provider.isPrototypeOf(options.tracerProvider));
  globalThis.__traceai_enable_calls = (globalThis.__traceai_enable_calls || 0) + 1;
  if (process.env.ENABLE_MODE === 'throw') throw new Error('broken optional entry');
  return { status: 'instrumented' };
};
