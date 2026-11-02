'use strict';
// Real ADK model/tool spans, synthetic content and a localhost OTLP collector.
// Usage: node native-privacy-wire.cjs PACKAGE_PATH ADK_RUNTIME MODE [WIRE_PATH]
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const [packagePath, runtime, mode, outputPath] = process.argv.slice(2);
const pkgRequire = createRequire(packagePath + '/package.json');
const { configure } = pkgRequire(packagePath);
const { trace } = pkgRequire('@opentelemetry/api');
const adkRequire = createRequire(runtime + '/package.json');
const hideModes = {
  'hide-inputs': 'FI_HIDE_INPUTS', 'hide-outputs': 'FI_HIDE_OUTPUTS',
  'hide-input-text': 'FI_HIDE_INPUT_TEXT', 'hide-output-text': 'FI_HIDE_OUTPUT_TEXT',
  'hide-input-messages': 'FI_HIDE_INPUT_MESSAGES', 'hide-output-messages': 'FI_HIDE_OUTPUT_MESSAGES',
  'pii': 'FI_PII_REDACTION', 'numeric-pii': 'FI_PII_REDACTION',
};
assert(mode === 'credentials' || mode === 'credential-variants' || mode === 'default' || hideModes[mode], 'Unknown probe mode');
const credentialVariants = {api_token: 'synthetic-api-token', session_token: 'synthetic-session-token', aws_secret_access_key: 'synthetic-aws-secret', AWS_ACCESS_KEY_ID: 'synthetic-aws-id', awsSessionToken: 'synthetic-aws-session'};
const numericPii = {timestamp: 1730572800000, accountNumber: 4111111111111111, ordinary: 'ordinary visible text', count: 42};
const extra = mode === 'credential-variants' ? {...credentialVariants, max_tokens: 42} : mode === 'numeric-pii' ? numericPii : {};

async function main() {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      requests.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.writeHead(200, {'content-type': 'application/json'}); res.end('{}');
    });
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  if (hideModes[mode]) process.env[hideModes[mode]] = 'true';
  const provider = configure({projectName: 'privacy-adk', endpoint: `http://127.0.0.1:${server.address().port}/v1/traces`, apiKey: 'synthetic-fi-api', secretKey: 'synthetic-fi-secret', captureContent: mode !== 'default'});
  try {
    const adk = adkRequire('@google/adk');
    class Model extends adk.BaseLlm {
      constructor() { super({model: 'gemini-synthetic'}); this.calls = 0; }
      async *generateContentAsync() {
        this.calls++;
        yield {
          content: {role: 'model', parts: this.calls === 1 ? [{functionCall: {id: 'call1', name: 'echo', args: {...extra, value: 'PRIVATE_TOOL', apiKey: 'synthetic-tool-api-key', authorization: 'Bearer synthetic-tool-token', customerEmail: 'synthetic.person@example.com'}}}] : [{text: 'PRIVATE_OUTPUT'}]},
          usageMetadata: {promptTokenCount: 10, candidatesTokenCount: 5, thoughtsTokenCount: 3, totalTokenCount: 18},
        };
      }
    }
    const tool = new adk.FunctionTool({name: 'echo', description: 'Synthetic echo', execute: async args => ({...extra, result: args.value, access_token: 'synthetic-output-access-token'})});
    const agent = new adk.LlmAgent({name: 'privacy_agent', model: new Model(), tools: [tool]});
    const runner = new adk.InMemoryRunner({agent, appName: 'privacy'});
    await runner.sessionService.createSession({appName: 'privacy', userId: 'synthetic-user', sessionId: 'PRIVATE_SESSION'});
    for await (const event of runner.runAsync({userId: 'synthetic-user', sessionId: 'PRIVATE_SESSION', newMessage: {role: 'user', parts: [{text: 'PRIVATE_INPUT synthetic.person@example.com'}]}})) {
      // Consume the complete run so final model/tool spans are exported.
    }
    await provider.forceFlush();
    const spans = requests.flatMap(r => r.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(ss => ss.spans)));
    const attrs = s => Object.fromEntries((s.attributes ?? []).map(a => [a.key, a.value.stringValue ?? a.value.intValue ?? a.value.doubleValue]));
    const json = JSON.stringify(requests);
    const llms = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
    const tools = spans.filter(s => attrs(s)['fi.span.kind'] === 'TOOL');
    assert.equal(spans.length, 5);
    assert.equal(llms.length, 2);
    assert.equal(tools.length, 1);
    assert.equal(new Set(spans.map(s => s.spanId)).size, spans.length);
    assert.equal(new Set(spans.map(s => s.traceId)).size, 1);
    assert.equal(trace.getActiveSpan(), undefined);
    if (mode === 'default') assert(!json.includes('PRIVATE_'));
    if (hideModes[mode] && mode !== 'numeric-pii') {
      const marker = mode.includes('output') ? 'PRIVATE_OUTPUT' : mode === 'pii' ? 'synthetic.person@example.com' : 'PRIVATE_INPUT';
      const field = mode.includes('output') ? 'output.value' : 'input.value';
      assert(llms.every(span => !attrs(span)[field]?.includes(marker)), 'Hidden content reached the controlled field');
      if (mode === 'pii') assert(!json.includes(marker));
    }
    for (const secret of ['synthetic-tool-api-key', 'synthetic-output-access-token', 'synthetic-tool-token']) {
      assert(!json.includes(secret), 'Credential reached OTLP, including model request history');
    }
    if (mode === 'credentials') {
      assert(attrs(tools[0])['input.value'].includes('PRIVATE_TOOL'));
      assert(attrs(llms.at(-1))['output.value'].includes('PRIVATE_OUTPUT'));
    }
    if (mode === 'credential-variants') {
      for (const secret of Object.values(credentialVariants)) assert(!json.includes(secret), 'Credential variant reached OTLP');
      for (const side of ['input', 'output']) {
        const payload = JSON.parse(attrs(tools[0])[`${side}.value`]);
        for (const key of Object.keys(credentialVariants)) assert.equal(payload[key], '__REDACTED__');
        assert.equal(payload.max_tokens, 42);
      }
      assert(attrs(llms.at(-1))['input.value'].includes('api_token'), 'Exercise subsequent model history');
    }
    if (mode === 'numeric-pii') {
      for (const pii of ['1730572800000', '4111111111111111', 'synthetic.person@example.com']) assert(!json.includes(pii));
      const hasScanner = typeof pkgRequire('@traceai/fi-core').redactPiiInString === 'function';
      for (const span of [...tools, ...llms]) {
        for (const side of ['input', 'output']) {
          const a = attrs(span);
          if (hasScanner) {
            assert.equal(a[`${side}.mime_type`], 'application/json');
            const payload = JSON.parse(a[`${side}.value`]);
            if (tools.includes(span)) {
              assert.equal(payload.timestamp, '<CREDIT_CARD>');
              assert.equal(payload.accountNumber, '<CREDIT_CARD>');
              assert.equal(payload.count, 42);
              assert.equal(payload.ordinary, numericPii.ordinary);
            }
          } else {
            assert.equal(a[`${side}.value`], '__REDACTED__', 'Older fi-core must fail closed');
            assert.equal(a[`${side}.mime_type`], undefined);
          }
        }
      }
    }
    for (const span of llms) {
      assert.equal(Number(attrs(span)['gen_ai.usage.total_tokens']), 18);
      assert.equal(Number(attrs(span)['gen_ai.usage.output_tokens']), 8);
    }
    if (outputPath) fs.writeFileSync(outputPath, JSON.stringify(requests, null, 2));
    console.log(JSON.stringify({mode, spanCount: spans.length, wirePrivacyChecks: 'passed'}));
  } finally {
    await provider.shutdown(); server.close(); await once(server, 'close');
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
