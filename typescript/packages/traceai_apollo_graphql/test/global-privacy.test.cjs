const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const cases = [
  { name: 'metadata-only default', options: {}, source: false, errors: false },
  { name: 'explicit capture opt-in', options: { captureContent: true }, source: true, errors: true },
  { name: 'environment capture opt-in', options: {}, env: { FI_APOLLO_CAPTURE_CONTENT: 'true' }, source: true, errors: true },
  ...['FI_HIDE_INPUTS', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_INPUT_TEXT'].map(flag => ({
    name: `${flag} overrides capture and derives hidden errors`, env: { [flag]: 'true' }, source: false, errors: false,
  })),
  ...['FI_HIDE_OUTPUTS', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_OUTPUT_TEXT'].map(flag => ({
    name: `${flag} derives hidden errors`, env: { [flag]: 'true' }, source: true, errors: false,
  })),
  { name: 'explicit error hiding', env: { FI_HIDE_ERROR_DETAILS: 'true' }, source: true, errors: false },
  { name: 'explicit false overrides derived error hiding, not input hiding',
    env: { FI_HIDE_INPUTS: 'true', FI_HIDE_ERROR_DETAILS: 'false' }, source: false, errors: true },
  { name: 'explicit false cannot opt into content', options: { captureContent: false },
    env: { FI_HIDE_ERROR_DETAILS: 'false', FI_APOLLO_CAPTURE_CONTENT: 'true' }, source: false, errors: false },
  { name: 'programmatic input hiding derives hidden errors', options: { captureContent: true, hideInputs: true },
    source: false, errors: false },
  { name: 'programmatic error hiding and PII redaction',
    options: { captureContent: true, hideErrorDetails: true, piiRedaction: true }, source: true, errors: false, pii: true },
  { name: 'PII redaction of retained text', env: { FI_PII_REDACTION: 'true' }, source: true, errors: true, pii: true },
  { name: 'PII redaction with explicit error retention',
    env: { FI_HIDE_INPUT_TEXT: 'true', FI_HIDE_ERROR_DETAILS: 'false', FI_PII_REDACTION: 'true' },
    source: false, errors: true, pii: true },
];
for (const scenario of cases) test(`exported Apollo spans: ${scenario.name}`, () => {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (key.startsWith('FI_HIDE_') || ['FI_PII_REDACTION', 'FI_APOLLO_CAPTURE_CONTENT'].includes(key)) delete env[key];
  }
  const options = scenario.options ?? { captureContent: true };
  const result = spawnSync(process.execPath, [path.join(__dirname, 'privacy-scenario.cjs')], {
    cwd: path.resolve(__dirname, '..'), encoding: 'utf8', timeout: 10000,
    env: { ...env, ...scenario.env, PRIVACY_OPTIONS: JSON.stringify(options) },
  });
  assert.equal(result.status, 0, result.stderr);
  const spans = JSON.parse(result.stdout.trim());
  assert.ok(spans.some(span => span.name === 'query Review'), 'real instrumented operation exported');
  const sources = spans.map(s => s.attributes['graphql.source']).filter(Boolean);
  const errors = spans.flatMap(s => s.events).map(e => e.attributes?.['exception.message']).filter(Boolean);
  const probe = spans.find(s => s.name === 'privacy-boundary');
  assert.equal(sources.length > 0, scenario.source);
  assert.equal(sources.join().includes('PRIVATE_ENUM'), scenario.source);
  assert.equal(errors.some(e => e.includes('PRIVATE_ERROR')), scenario.errors);
  assert.equal(Boolean(probe.status.message), scenario.errors);
  assert.equal(probe.status.code, 2);
  assert.ok(spans.flatMap(s => s.events).some(e => e.attributes?.['exception.type'] === 'Error'));
  if (scenario.pii) {
    assert.doesNotMatch(JSON.stringify(spans), /review\.person@example\.test/);
    assert.match(JSON.stringify(spans), /<EMAIL_ADDRESS>/);
  }
});
