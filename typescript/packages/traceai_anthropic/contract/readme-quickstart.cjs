'use strict';
// Execute the README, not a copied setup, against real SDKs and loopback HTTP.
// Run after building the workspace, with external networking denied by the caller.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const ts = require('typescript');

const packageDir = path.resolve(__dirname, '..');
const readme = fs.readFileSync(path.join(packageDir, 'README.md'), 'utf8');
const quickStart = readme.split('## Quick Start\n')[1]?.split('\n## ')[0];
const sample = quickStart?.match(/```typescript\n([\s\S]*?)```/)?.[1];
assert.ok(sample, 'README must contain the executable Quick Start');

async function runSample(filename, url) {
  // No inherited credentials, provider settings, or runtime preloads.
  const child = spawn(process.execPath, [filename], {
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      FI_BASE_URL: url,
      FI_API_KEY: 'synthetic-fi-key',
      FI_SECRET_KEY: 'synthetic-fi-secret',
      ANTHROPIC_API_KEY: 'synthetic-anthropic-key',
      ANTHROPIC_BASE_URL: url,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  child.stderr.on('data', chunk => { stderr += chunk; });
  const timer = setTimeout(() => child.kill('SIGKILL'), 30_000);
  try {
    const code = await new Promise((resolve, reject) => {
      child.on('error', reject);
      child.on('close', resolve);
    });
    assert.equal(code, 0, `${path.basename(filename)} failed: ${stdout}\n${stderr}`);
  } finally {
    clearTimeout(timer);
  }
}

async function main() {
  // Keeping samples beneath the package preserves normal import/require resolution.
  const sampleDir = fs.mkdtempSync(path.join(packageDir, '.readme-contract-'));
  const requests = [];
  const payloads = [];
  const unexpected = [];
  const receiver = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => {
      response.setHeader('content-type', 'application/json');
      const body = JSON.parse(Buffer.concat(chunks).toString());
      if (request.method === 'POST' && request.url === '/v1/messages') {
        requests.push(body);
        response.end(JSON.stringify({
          id: 'msg_readme', type: 'message', role: 'assistant', model: body.model,
          content: [{ type: 'text', text: 'Synthetic response' }],
          stop_reason: 'end_turn', stop_sequence: null,
          usage: { input_tokens: 7, output_tokens: 3 },
        }));
      } else if (request.method === 'POST' && request.url === '/tracer/v1/traces') {
        payloads.push(body);
        response.end('{}');
      } else {
        unexpected.push(`${request.method} ${request.url}`);
        response.writeHead(404).end('{}');
      }
    });
  });
  const results = [];
  try {
    await new Promise(resolve => receiver.listen(0, '127.0.0.1', resolve));
    const url = `http://127.0.0.1:${receiver.address().port}`;
    for (const mode of ['esm', 'commonjs']) {
      requests.length = payloads.length = unexpected.length = 0;
      const esm = mode === 'esm';
      const compiled = ts.transpileModule(sample, { compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: esm ? ts.ModuleKind.ES2022 : ts.ModuleKind.CommonJS,
        esModuleInterop: true,
      } }).outputText;
      const filename = path.join(sampleDir, esm ? 'quickstart.mjs' : 'quickstart.cjs');
      // CommonJS does not support top-level await; only wrap its executable body.
      fs.writeFileSync(filename, esm ? compiled :
        `(async () => {\n${compiled}\n})().catch(error => { console.error(error); process.exitCode = 1; });\n`);
      await runSample(filename, url);
      assert.deepEqual(unexpected, [], `${mode}: unexpected HTTP route`);
      assert.equal(requests.length, 1, `${mode}: exactly one SDK request`);
      const spans = payloads.flatMap(p => p.resourceSpans)
        .flatMap(r => r.scopeSpans).flatMap(s => s.spans);
      results.push({ mode, requests: requests.length, spans: spans.length });
      // Validate actual exported attributes, not just a successful request.
      for (const span of spans) {
        const attributes = Object.fromEntries(span.attributes.map(a => [a.key, a.value]));
        assert.equal(attributes['fi.span.kind']?.stringValue, 'LLM');
        assert.equal(attributes['gen_ai.request.model']?.stringValue, requests[0].model);
        assert.equal(Number(attributes['gen_ai.usage.input_tokens']?.intValue), 7);
        assert.equal(Number(attributes['gen_ai.usage.output_tokens']?.intValue), 3);
      }
    }
    console.log(JSON.stringify({ cases: results }, null, 2));
    for (const result of results) {
      assert.equal(result.spans, 1, `${result.mode}: README must export exactly one span`);
    }
  } finally {
    await new Promise(resolve => receiver.close(resolve));
    fs.rmSync(sampleDir, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
