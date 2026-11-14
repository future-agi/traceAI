import { spawn } from 'node:child_process';
import { createServer, Server } from 'node:http';
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import type { AddressInfo } from 'node:net';

// With no application OpenTelemetry, the SDK request must arrive only as its model
// span. A plain fetch started beside it still needs its own wire span.
const root = resolve(__dirname, '../..');
const auto = resolve(root, '../traceai_auto/src/register.ts');
const SDK_PROMPT = 'question sent through the Portkey SDK', PLAIN_PROMPT = 'question sent with a plain fetch';
const BOTH = [PLAIN_PROMPT, SDK_PROMPT].sort();
const completion = { id: 'chat-fixture', object: 'chat.completion', created: 0, model: 'fixture', choices: [{ index: 0, message: { role: 'assistant', content: 'hello world' }, finish_reason: 'stop' }], usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 } };
let server: Server, url: string, collected: any[], received: string[];
const attrs = (span: any): Record<string, any> => Object.fromEntries((span.attributes ?? []).map((item: any) => [
  item.key, item.value.stringValue ?? item.value.intValue ?? item.value.boolValue ?? item.value.arrayValue,
]));
const exportedSpans = () => collected.flatMap(batch => batch.resourceSpans ?? [])
  .flatMap(resource => resource.scopeSpans ?? []).flatMap(scope => scope.spans ?? []);

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      const body = Buffer.concat(chunks).toString();
      if (req.url === '/tracer/v1/traces') {
        collected.push(JSON.parse(body));
        res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}'); return;
      }
      // Both clients target the loopback provider's OpenAI-compatible chat route.
      if (req.method !== 'POST' || req.url !== '/v1/chat/completions') {
        received.push(`unexpected ${req.method} ${req.url}`);
        res.writeHead(400, { 'content-type': 'application/json' }); res.end('{"error":{"message":"unexpected fixture request"}}'); return;
      }
      const data = JSON.parse(body);
      received.push(String(data.messages?.[0]?.content));
      res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(completion));
    });
  });
  await new Promise<void>(done => server.listen(0, '127.0.0.1', done));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => {
  server.closeAllConnections();
  await new Promise<void>(done => server.close(() => done()));
});
beforeEach(() => { collected = []; received = []; });

// Ordinary Node resolution lets the real import hooks see portkey-ai. Only the
// instrumentation's source entry is redirected; neither SDK nor hooks are mocked.
async function child(traced: boolean): Promise<any> {
  const scratch = mkdtempSync(join(root, '.traceai-portkey-beside-'));
  try {
    const modules = join(scratch, 'node_modules');
    mkdirSync(join(modules, '@traceai/portkey'), { recursive: true });
    const sdkDir = dirname(realpathSync(require.resolve('portkey-ai/package.json')));
    symlinkSync(sdkDir, join(modules, 'portkey-ai'), 'dir');
    writeFileSync(join(modules, '@traceai/portkey/package.json'), JSON.stringify({
      name: '@traceai/portkey', main: join(root, 'src/index.ts'),
    }));
    const app = join(scratch, 'app.cjs');
    writeFileSync(app, readFileSync(join(__dirname, 'beside-portkey-call-no-otel-app.cjs')));
    const env = { ...process.env };
    for (const key of Object.keys(env)) {
      // No inherited credentials, preload, privacy override, collector, or TS loader.
      if (/^(?:FI_|OTEL_|TS_NODE_|PORTKEY_)/.test(key) || /KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH/i.test(key) || key === 'NODE_OPTIONS') delete env[key];
    }
    Object.assign(env, { NODE_OPTIONS: '', FI_API_KEY: 'sk-test-PLACEHOLDER', FI_SECRET_KEY: 'sk-test-PLACEHOLDER',
      FI_BASE_URL: url, FI_INSTRUMENTATIONS: 'portkey,wire', FI_PROJECT_NAME: 'portkey-no-otel-test',
      MOCK_URL: url, SDK_PROMPT, PLAIN_PROMPT });
    const args = ['--no-deprecation', '-r', join(__dirname, 'no-otel-preload.cjs'), ...(traced ? ['-r', auto] : []), app, traced ? 'traced' : 'baseline'];
    const output = await new Promise<string>((done, reject) => {
      const proc = spawn(process.execPath, args, { cwd: scratch, env, stdio: ['ignore', 'pipe', 'pipe'] });
      let stdout = '', stderr = '';
      const timer = setTimeout(() => { proc.kill('SIGKILL'); reject(new Error(`Child timed out: ${stderr}\n${stdout}`)); }, 20000);
      proc.stdout.on('data', chunk => { stdout += chunk; });
      proc.stderr.on('data', chunk => { stderr += chunk; });
      proc.on('error', error => { clearTimeout(timer); reject(error); });
      proc.on('close', code => {
        clearTimeout(timer);
        if (code !== 0) reject(new Error(`Child exited ${code}: ${stderr}\n${stdout}`));
        else done(stdout.trim());
      });
    });
    return JSON.parse(output.split('\n').at(-1)!);
  } finally { rmSync(scratch, { recursive: true, force: true }); }
}

test('without application OpenTelemetry, a Portkey call is captured once and a plain fetch beside it is not lost', async () => {
  const baseline = await child(false);
  expect(baseline.application).toEqual({ text: 'hello world', plain: 'hello world' });
  expect(baseline.contextManager).toBe('NoopContextManager');
  expect([...received].sort()).toEqual(BOTH);
  expect(collected).toHaveLength(0);
  received = [];

  const actual = await child(true);
  expect(actual.application).toEqual(baseline.application);
  expect([...received].sort()).toEqual(BOTH);
  // Portkey discovers resources through its constructor wrapper rather than _wrap.
  // Both patch-reporting statuses require the actual model span below.
  expect({ active: actual.active, portkey: actual.portkey, contextManager: actual.contextManager })
    .toEqual({ active: true, portkey: expect.stringMatching(/^(?:instrumented|manual)$/), contextManager: 'NoopContextManager' });

  const spans = exportedSpans();
  const kind = (span: any) => attrs(span)['gen_ai.span.kind'] ?? attrs(span)['fi.span.kind'];
  const got = spans.map(span => `${span.name} (${attrs(span)['traceai.capture.layer']}, ${kind(span)})`);
  const carries = (span: any, text: string) => JSON.stringify(span).includes(text);
  const model = spans.filter(span => attrs(span)['traceai.capture.layer'] !== 'wire'
    && attrs(span)['gen_ai.provider.name'] === 'portkey' && kind(span) === 'LLM' && carries(span, SDK_PROMPT));
  const sdkWire = spans.filter(span => attrs(span)['traceai.capture.layer'] === 'wire' && carries(span, SDK_PROMPT));
  const plain = spans.filter(span => attrs(span)['traceai.capture.layer'] === 'wire' && carries(span, PLAIN_PROMPT));
  expect({ got, modelSpans: model.length, sdkWireSpans: sdkWire.length, plainFetchWireSpans: plain.length })
    .toEqual({ got, modelSpans: 1, sdkWireSpans: 0, plainFetchWireSpans: 1 });
}, 60000);
