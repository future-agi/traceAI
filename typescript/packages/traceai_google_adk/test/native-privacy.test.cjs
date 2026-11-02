'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { GoogleADKSpanExporter } = require('../dist/src');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');

const flags = ['FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_OUTPUT_TEXT', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_INPUT_IMAGES', 'FI_PII_REDACTION'];
async function exported(flag, operation, input, output) {
  const saved = Object.fromEntries(flags.map(key => [key, process.env[key]]));
  const spans = [];
  let provider;
  try {
    flags.forEach(key => delete process.env[key]);
    if (flag) process.env[flag] = 'true';
    const exporter = new GoogleADKSpanExporter({export(batch, done) { spans.push(...batch); done({code: 0}); }, shutdown: async () => {}}, {captureContent: true});
    provider = new BasicTracerProvider({spanProcessors: [new SimpleSpanProcessor(exporter)]});
    const tool = operation === 'execute_tool';
    const attributes = {
      'gen_ai.operation.name': operation,
      [tool ? 'gcp.vertex.agent.tool_call_args' : 'gcp.vertex.agent.llm_request']: typeof input === 'string' ? input : JSON.stringify(input),
      [tool ? 'gcp.vertex.agent.tool_response' : 'gcp.vertex.agent.llm_response']: typeof output === 'string' ? output : JSON.stringify(output),
    };
    const original = provider.getTracer('gcp.vertex.agent').startSpan(tool ? 'tool' : 'call_llm', {attributes});
    original.end();
    await provider.forceFlush();
    assert.deepEqual(original.attributes, attributes, 'never mutate the SDK span');
    assert.equal(spans.length, 1);
    return spans[0].attributes;
  } finally {
    await provider?.shutdown();
    flags.forEach(key => saved[key] === undefined ? delete process.env[key] : process.env[key] = saved[key]);
  }
}

const input = {contents: [{role: 'user', parts: [{text: 'PRIVATE_INPUT'}, {functionResponse: {response: {nested: ['PRIVATE_INPUT']}}}]}], config: {systemInstruction: 'PRIVATE_INPUT'}};
const output = {content: {role: 'model', parts: [{text: 'PRIVATE_OUTPUT'}, {functionCall: {args: {nested: ['PRIVATE_OUTPUT']}}}]}, usageMetadata: {promptTokenCount: 11, candidatesTokenCount: 7, totalTokenCount: 18}};
for (const flag of flags.slice(0, 6)) {
  test(`${flag} masks native model and tool payloads, including nested strings`, async () => {
    const side = flag.includes('OUTPUT') ? 'output' : 'input';
    const marker = side === 'input' ? 'PRIVATE_INPUT' : 'PRIVATE_OUTPUT';
    for (const operation of ['generate_content', 'execute_tool']) {
      const a = await exported(flag, operation, input, output);
      assert(!String(a[`${side}.value`]).includes(marker));
      const opposite = side === 'input' ? 'output' : 'input';
      assert(String(a[`${opposite}.value`]).includes(`PRIVATE_${opposite.toUpperCase()}`));
      if (flag === 'FI_HIDE_INPUTS' || flag === 'FI_HIDE_OUTPUTS') {
        assert.equal(a[`${side}.value`], '__REDACTED__');
        assert.equal(a[`${side}.mime_type`], undefined);
      }
      if (operation !== 'execute_tool') assert.equal(a['gen_ai.usage.total_tokens'], 18);
    }
  });
}

test('FI_PII_REDACTION removes PII from nested native model/tool JSON', async () => {
  for (const operation of ['generate_content', 'execute_tool']) {
    const payload = {content: {parts: [{text: 'Contact synthetic.person@example.com'}]}, nested: ['synthetic.person@example.com']};
    const a = await exported('FI_PII_REDACTION', operation, payload, payload);
    assert(!JSON.stringify(a).includes('synthetic.person@example.com'));
    assert(a['input.value'].includes('<EMAIL_ADDRESS>'));
    assert(a['output.value'].includes('<EMAIL_ADDRESS>'));
  }
});

test('credentials are always redacted recursively, even without a hide flag', async () => {
  const credentials = {apiKey: 'synthetic-api', authorization: 'Bearer synthetic-auth', access_token: 'synthetic-access', refreshToken: 'synthetic-refresh', client_secret: 'synthetic-secret', password: 'synthetic-password', 'X-API-Key': 'synthetic-header', private_key: 'synthetic-private'};
  const payload = {contents: [{parts: [{functionCall: {args: credentials}}, {functionResponse: {response: credentials}}, {text: 'Authorization: Bearer synthetic-text-auth~synthetic-suffix'}, {text: 'Basic synthetic-basic=='}]}], nested: [credentials], encoded: JSON.stringify(credentials), result: 'ordinary visible content', count: 42};
  for (const operation of ['generate_content', 'execute_tool']) {
    const a = await exported(undefined, operation, payload, payload);
    assert(!JSON.stringify(a).includes('synthetic-'), 'credentials must not reach the exporter');
    const safe = JSON.parse(a['input.value']);
    assert.equal(safe.result, payload.result);
    assert.equal(safe.count, 42);
    assert.equal(safe.nested[0].apiKey, '__REDACTED__');
    assert.equal(a['input.mime_type'], 'application/json');
  }
});

test('common token and AWS credential fields are redacted without hiding token counts', async () => {
  const credentials = {
    api_token: 'synthetic-api-token', session_token: 'synthetic-session-token',
    aws_secret_access_key: 'synthetic-aws-secret', AWS_ACCESS_KEY_ID: 'synthetic-aws-id',
    awsSessionToken: 'synthetic-aws-session', 'X-API-Token': 'synthetic-header-token',
    security_token: 'synthetic-security-token', bearerToken: 'synthetic-bearer-token',
  };
  const counts = {max_tokens: 42, promptTokenCount: 11, total_tokens: 18, token_count: 7};
  const payload = {credentials, nested: [credentials], encoded: JSON.stringify(credentials), counts};
  for (const operation of ['generate_content', 'execute_tool']) {
    const a = await exported(undefined, operation, payload, payload);
    for (const side of ['input', 'output']) {
      assert(!a[`${side}.value`].includes('synthetic-'));
      const safe = JSON.parse(a[`${side}.value`]);
      assert.deepEqual(safe.counts, counts);
      for (const key of Object.keys(credentials)) {
        assert.equal(safe.nested[0][key], '__REDACTED__');
        assert.equal(JSON.parse(safe.encoded)[key], '__REDACTED__');
      }
    }
  }
});

test('PII redaction preserves JSON structure for numeric values and encoded payloads', async () => {
  const data = {timestamp: 1730572800000, accountNumber: 4111111111111111, ordinary: 'ordinary visible text', count: 42, enabled: true, absent: null};
  const payload = {...data, array: [data.accountNumber, data.count, data.enabled, data.absent], encoded: JSON.stringify(data), 'synthetic.person@example.com': 'email key', text: 'quoted "text" and \\ backslash'};
  for (const operation of ['generate_content', 'execute_tool']) {
    const a = await exported('FI_PII_REDACTION', operation, payload, payload);
    for (const side of ['input', 'output']) {
      assert.equal(a[`${side}.mime_type`], 'application/json');
      const safe = JSON.parse(a[`${side}.value`]);
      assert.equal(safe.timestamp, '<CREDIT_CARD>');
      assert.equal(safe.accountNumber, '<CREDIT_CARD>');
      assert.equal(safe.ordinary, data.ordinary);
      assert.equal(safe.count, 42);
      assert.equal(safe.enabled, true);
      assert.equal(safe.absent, null);
      assert.equal(safe.text, payload.text);
      assert.deepEqual(safe.array, ['<CREDIT_CARD>', 42, true, null]);
      assert.equal(JSON.parse(safe.encoded).accountNumber, '<CREDIT_CARD>');
      assert.equal(safe['<EMAIL_ADDRESS>'], 'email key');
      for (const pii of ['1730572800000', '4111111111111111', 'synthetic.person@example.com']) {
        assert(!a[`${side}.value`].includes(pii));
      }
    }
  }
});

test('FI_HIDE_INPUT_IMAGES removes native inline and file data recursively', async () => {
  const payload = {contents: [{parts: [{inlineData: {mimeType: 'image/png', data: 'PRIVATE_IMAGE'}}, {fileData: {mimeType: 'image/png', fileUri: 'PRIVATE_IMAGE'}}, {text: 'visible'}]}]};
  const a = await exported('FI_HIDE_INPUT_IMAGES', 'generate_content', payload, output);
  assert(!a['input.value'].includes('PRIVATE_IMAGE'));
  assert(a['input.value'].includes('visible'));
});

test('malformed or excessively nested content fails closed without losing the span', async () => {
  const malformed = '{"apiKey":"synthetic-malformed"';
  let deep = {apiKey: 'synthetic-deep'};
  for (let i = 0; i < 100; i++) deep = {nested: deep};
  for (const value of [malformed, deep]) {
    const a = await exported(undefined, 'execute_tool', value, value);
    assert(!JSON.stringify(a).includes('synthetic-'));
    assert.equal(a['fi.span.kind'], 'TOOL');
  }
});
