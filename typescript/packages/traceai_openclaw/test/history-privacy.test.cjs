const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { resolveConfig } = require('@traceai/agent-core');
const { normalizeSpan } = require('../dist/normalize.js');

function span(name, attributes) {
  return {
    name, attributes, status: { code: 1 },
    spanContext: () => ({ traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1 }),
  };
}
const messages = JSON.stringify([
  { role: 'user', content: 'USER_PROMPT' },
  { role: 'assistant', content: 'PRIOR_ASSISTANT_OUTPUT' },
  { role: 'tool', content: 'PRIOR_TOOL_OUTPUT' },
  { role: 'tool', isError: true, content: 'HISTORICAL_TOOL_ERROR' },
]);
const source = span('openclaw.model.call', {
  'openclaw.model_call.observation_unit': 'request',
  'openclaw.content.input_messages': messages,
  'openclaw.content.output_messages': 'CURRENT_SUCCESSFUL_OUTPUT',
  'gen_ai.usage.input_tokens': 11,
});

for (const flag of ['FI_HIDE_OUTPUTS', 'FI_HIDE_OUTPUT_TEXT', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_ERROR_DETAILS']) {
  test(flag + ' cannot leak historical output/error content through native input messages', async t => {
    const home = await fs.mkdtemp(join(tmpdir(), 'fi-openclaw-history-'));
    t.after(() => fs.rm(home, { recursive: true, force: true }));
    // Exercise the actual env aliases; explicitly expose errors in the output
    // cases so their protection cannot accidentally rely on hideErrorDetails.
    const config = await resolveConfig({ agent: 'openclaw', home,
      env: { FI_HIDE_ERROR_DETAILS: 'false', [flag]: 'true' } });
    assert.equal(config.hideInputs, false);
    for (const input of [messages, JSON.stringify({ messages }), messages.slice(0, -10),
      'unstructured history: PRIOR_ASSISTANT_OUTPUT HISTORICAL_TOOL_ERROR']) {
      const normalized = normalizeSpan({ ...source,
        attributes: { ...source.attributes, 'openclaw.content.input_messages': input } }, config);
      assert.equal(normalized.attributes['input.value'], undefined);
      assert.equal(normalized.attributes['output.value'],
        flag === 'FI_HIDE_ERROR_DETAILS' ? 'CURRENT_SUCCESSFUL_OUTPUT' : undefined);
      assert.equal(normalized.attributes['gen_ai.usage.input_tokens'], 11);
      assert.equal(normalized.status.code, 1, 'historical errors do not fail a successful request');
      assert.ok(!JSON.stringify(normalized).includes('PRIOR_'));
      assert.ok(!JSON.stringify(normalized).includes('HISTORICAL_TOOL_ERROR'));
    }
    const tool = normalizeSpan(span('openclaw.tool.execution', {
      'openclaw.content.tool_input': '{"path":"USER_TOOL_ARGUMENT"}',
      'openclaw.content.tool_output': 'SUCCESSFUL_TOOL_OUTPUT',
    }), config);
    assert.equal(tool.attributes['input.value'], '{"path":"USER_TOOL_ARGUMENT"}');
    assert.equal(tool.attributes['output.value'], flag === 'FI_HIDE_ERROR_DETAILS' ? 'SUCCESSFUL_TOOL_OUTPUT' : undefined);
  });
}

test('default capture retains mixed history without modifying the source', async t => {
  const home = await fs.mkdtemp(join(tmpdir(), 'fi-openclaw-history-default-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const config = await resolveConfig({ agent: 'openclaw', home, env: {} });
  const before = JSON.stringify(source);
  const normalized = normalizeSpan(source, config);
  assert.equal(normalized.attributes['input.value'], messages);
  assert.equal(normalized.attributes['output.value'], 'CURRENT_SUCCESSFUL_OUTPUT');
  assert.equal(JSON.stringify(source), before);
});
