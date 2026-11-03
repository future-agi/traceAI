// Runs the packed README without editing its application or provider setup.
// Only Google's HTTP transport is replaced; OTLP uses real loopback HTTP.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const consumer = createRequire(path.join(process.env.GENAI_TEST_CONSUMER, 'package.json'));
const readme = fs.readFileSync(path.join(
  process.env.GENAI_TEST_CONSUMER, 'node_modules/@traceai/google-genai/README.md',
), 'utf8');
const blocks = [...readme.matchAll(/```javascript\n([\s\S]*?)```/g)].map(match => match[1]);
assert.equal(blocks.length, 3, 'README setup and privacy examples must be exercised');
let code = blocks[0];
if (process.argv[2] === 'collector') {
  code = code.replace(/  const provider = register\(\{[\s\S]*?\n  \}\);/, blocks[1]);
  assert.ok(!code.includes('const provider = register('));
  if (process.env.WIRE_FORMAT === 'protobuf') {
    code = code.replace('@opentelemetry/exporter-trace-otlp-http', '@opentelemetry/exporter-trace-otlp-proto');
  }
}
if (process.env.TEST_HIDE_CONTENT === 'true') {
  code = code.replace('const instrumentation = new GoogleGenAIInstrumentation();', blocks[2]);
}
global.fetch = async (url) => {
  assert.match(String(url), /^https:\/\/generativelanguage\.googleapis\.com\//);
  return new Response(JSON.stringify({
    candidates: [{ content: { role: 'model', parts: [{ text: 'synthetic-answer' }] }, finishReason: 'STOP' }],
    usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 2, totalTokenCount: 7 },
  }), { headers: { 'content-type': 'application/json' } });
};
(async () => {
  await vm.runInNewContext(code, { require: consumer, process, console });
  assert.notEqual(process.exitCode, 1, 'README main() must complete successfully');
})().catch(error => { console.error(error); process.exitCode = 1; });
