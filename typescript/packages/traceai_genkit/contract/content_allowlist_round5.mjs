/** Source-derived semantic inventory -> native Genkit -> complete loopback OTLP. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawnSync } from 'node:child_process';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createHash } from 'node:crypto';
import * as semconv from '@traceai/fi-semantic-conventions';
import { context, trace } from '@opentelemetry/api';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { genkit } from 'genkit';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { register, ProjectType, setAttributes } from '@traceai/fi-core';
import { FIGenkitSpanProcessor } from '@traceai/genkit';

const pkg = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const packages = path.resolve(pkg, '..');
const namespace = /^(?:gen_ai|llm|input|output|tool|tool_call|agent|fi\.agent|message|messages|message_content|audio|image|prompt|embedding|retrieval|reranker|document|raw\.input|raw\.output)(?:\.|$)/;
const inventory = new Map();
function add(key, source) {
  if (typeof key !== 'string' || !namespace.test(key) || !/^[a-z][a-z0-9_.]+$/.test(key)) return;
  key = key.replace(/\.$/, '');
  inventory.set(key, [...(inventory.get(key) ?? []), source]);
}
for (const [name, key] of Object.entries(semconv.SemanticConventions)) add(key, `SemanticConventions.${name}`);
// Actual flattened collection shapes compose a collection root with the
// exported element constants. Testing only invented .content leaves misses
// mixed namespaces such as embedding.embeddings.0.embedding.text.
const sc = semconv.SemanticConventions;
const fields = prefix => Object.entries(sc).filter(([name]) => name.startsWith(prefix));
function collection(root, elements) {
  for (const [name, key] of elements) add(`${root}.0.${key}`, `flattened ${root} + SemanticConventions.${name}`);
}
for (const root of [sc.LLM_INPUT_MESSAGES, sc.LLM_OUTPUT_MESSAGES]) {
  collection(root, fields('MESSAGE_'));
  collection(`${root}.0.${sc.MESSAGE_CONTENTS}`, fields('MESSAGE_CONTENT_'));
  collection(`${root}.0.${sc.MESSAGE_TOOL_CALLS}`, fields('TOOL_CALL_'));
}
collection(sc.LLM_TOOLS, fields('TOOL_'));
collection(sc.EMBEDDING_EMBEDDINGS, fields('EMBEDDING_'));
for (const root of [sc.RETRIEVAL_DOCUMENTS, sc.RERANKER_INPUT_DOCUMENTS, sc.RERANKER_OUTPUT_DOCUMENTS]) collection(root, fields('DOCUMENT_'));
// Legacy attribute spellings are composed from the repository's exported constants.
for (const postfix of Object.values(semconv.LLMAttributePostfixes)) add(`${semconv.SemanticAttributePrefixes.llm}.${postfix}`, 'LLMAttributePostfixes');
function scan(dir) {
  for (const item of readdirSync(dir, { withFileTypes: true })) {
    if (item.name === '__tests__' || item.name === 'content-policy.ts') continue;
    const file = path.join(dir, item.name);
    if (item.isDirectory()) scan(file);
    else if (item.name.endsWith('.ts')) {
      for (const match of readFileSync(file, 'utf8').matchAll(/["']([a-z][a-z0-9_.]+)["']/g)) add(match[1], path.relative(packages, file));
    }
  }
}
scan(path.join(packages, 'fi-core/src'));
scan(path.join(pkg, 'src'));
// Future namespace members deliberately absent from today's constants, plus
// operation-dependent transcripts. Their fallback must not depend on a catalog.
for (const key of ['gen_ai.agent.input', 'gen_ai.agent.output', 'gen_ai.agent.description', 'gen_ai.future_payload',
  'llm.future_payload', 'agent.future_payload', 'tool.future_payload', 'messages', 'audio.transcript']) add(key, 'forward-compatibility probe');

// Independent contract oracle. Exact metadata, never a namespace-wide exemption.
const metadata = new Set([
  'gen_ai.span.kind', 'gen_ai.operation.name', 'gen_ai.system', 'gen_ai.provider.name', 'gen_ai.request.model',
  'gen_ai.response.model', 'gen_ai.response.id', 'gen_ai.response.finish_reasons', 'gen_ai.conversation.id',
  'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'gen_ai.usage.total_tokens', 'gen_ai.usage.input_tokens.audio',
  'gen_ai.usage.output_tokens.audio', 'gen_ai.usage.output_tokens.reasoning', 'gen_ai.usage.cache_read_tokens',
  'gen_ai.usage.cache_write_tokens', 'gen_ai.usage.cache_creation_tokens', 'gen_ai.usage.cache_read_tokens.audio', 'gen_ai.usage.provider_total_tokens',
  'gen_ai.cost.input', 'gen_ai.cost.output', 'gen_ai.cost.total', 'gen_ai.response.latency', 'gen_ai.response.time_to_first_chunk',
  'gen_ai.response.time_to_first_text', 'gen_ai.response.time_to_first_token', 'gen_ai.server.time_to_first_token',
  'gen_ai.server.time_per_output_token', 'gen_ai.server.request.duration', 'gen_ai.tool.name', 'gen_ai.tool.type', 'gen_ai.tool.call.id',
  'llm.model_name', 'llm.provider', 'llm.system', 'llm.prompt_template.version', 'llm.token_count.prompt', 'llm.token_count.completion', 'llm.token_count.total',
  'tool.name', 'tool_call.id', 'tool_call.function.name', 'message.role', 'message.name', 'message.tool_call_id', 'message.function_call_name',
  'message_content.type', 'embedding.model_name', 'reranker.model_name', 'reranker.top_k', 'document.id', 'document.score', 'prompt.vendor', 'prompt.id',
]);
// fi.agent's documented provenance constants are metadata, except user tags.
for (const [name, key] of Object.entries(semconv.SemanticConventions)) if (name.startsWith('FI_AGENT_') && name !== 'FI_AGENT_TAGS') metadata.add(key);
function under(key, roots) { return roots.some(root => key === root || key.startsWith(root + '.') || key.startsWith(root + '[')); }
function side(key) {
  if (metadata.has(key)) return 'metadata';
  // Array notation is a path separator, not part of the leaf's spelling.
  key = key.replace(/\[(\d+)\]/g, '.$1');
  if (under(key, ['gen_ai.tool.call.arguments', 'tool.input', 'tool.arguments', 'agent.input', 'gen_ai.agent.input', 'agent.instructions', 'gen_ai.agent.instructions', 'reranker.input_documents', 'reranker.query'])) return 'input';
  if (under(key, ['gen_ai.tool.call.result', 'tool.output', 'tool.result', 'agent.output', 'gen_ai.agent.output', 'gen_ai.tool.call', 'tool_call', 'llm.function_call', 'reranker.output_documents', 'retrieval', 'gen_ai.retrieval'])) return 'output';
  if (/^(?:gen_ai\.)?embedding(?:s)?(?:\.|$)/.test(key)) {
    if (/(?:^|\.)text(?:\.|$)/.test(key)) return 'input';
    const field = key.replace(/^embedding(?:\.|$)/, '');
    if (/(?:^|\.)vector(?:\.|$)/.test(field) || /(?:^|\.)embedding(?:\.\d+)*$/.test(field)) return 'output';
    return 'both';
  }
  if (under(key, ['input', 'raw.input', 'gen_ai.input', 'gen_ai.prompt', 'gen_ai.prompts', 'gen_ai.system_instructions', 'gen_ai.request', 'gen_ai.tool.definitions', 'gen_ai.tool.description', 'llm.input_messages', 'llm.prompts', 'llm.prompt_template', 'llm.invocation_parameters', 'llm.tools', 'tool.description', 'tool.parameters', 'tool.json_schema', 'prompt', 'image'])) return 'input';
  if (under(key, ['output', 'raw.output', 'gen_ai.output', 'gen_ai.completion', 'gen_ai.completions', 'gen_ai.response', 'llm.output_messages'])) return 'output';
  return 'both';
}
// The independent, always-on S6 media contract omits raw audio payload fields
// even with every hide flag off. Transcript text and exact usage counters are
// metadata boundaries in that catalog, not raw media. These rows still receive
// the marker and MUST omit it by default; they are not positive-export controls.
function defaultMediaOmission(key) {
  return /(?:^|\.)audio(?:\.|\[|$)/.test(key) && !metadata.has(key) && !/(?:^|\.)transcript(?:\.|\[|$)/.test(key);
}
const rows = [...inventory].sort(([a], [b]) => a.localeCompare(b)).flatMap(([key, sources]) =>
  [key, `${key}.0.content`, `${key}[0].content`].map(key => ({ key, sources, side: side(key), defaultMediaOmission: defaultMediaOmission(key), marker: 'SYNTHETIC CONTENT ! ' + createHash('sha256').update(key).digest('hex').slice(0, 20) })));
const flags = ['off', 'hideInputs', 'hideOutputs', 'hideInputMessages', 'hideOutputMessages', 'hideInputText', 'hideOutputText'];
const cases = ['span', 'context', 'event', 'link', 'resource'].flatMap(surface => flags.map(flag => ({ name: `${surface}-${flag}`, surface, flag })));
if (!process.argv[2]) {
  let failed = 0;
  for (const test of cases) {
    const child = spawnSync(process.execPath, [fileURLToPath(import.meta.url), test.name], { env: process.env, encoding: 'utf8', timeout: 180000, maxBuffer: 32 * 1024 * 1024 });
    process.stdout.write(child.stdout ?? '');
    if (child.status !== 0) { failed++; process.stderr.write(`${test.name}: ${child.error ?? child.stderr}\n`); }
  }
  console.log(JSON.stringify({ cases: cases.length, passed: cases.length - failed, failed, attributes: rows.length }));
  process.exitCode = failed ? 1 : 0;
} else {
  const test = cases.find(t => t.name === process.argv[2]); assert.ok(test);
  for (const key of Object.keys(process.env)) if (key.startsWith('FI_') || key.startsWith('OTEL_') || key === 'GENKIT_TELEMETRY_SERVER') delete process.env[key];
  const payloads = [], requests = [], native = [];
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks)); res.setHeader('Content-Type', 'application/json');
    if (req.url === '/model') { requests.push(body); res.end(JSON.stringify({ message: { role: 'model', content: [{ text: 'public reply' }] }, finishReason: 'stop' })); }
    else { payloads.push(body); res.end('{}'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const value = row => ['gen_ai.input.messages', 'gen_ai.output.messages'].includes(row.key)
    ? JSON.stringify([{ role: 'user', content: [{ type: 'text', text: row.marker }] }]) : row.marker;
  const resource = resourceFromAttributes({ 'service.name': 'public-service', ...(test.surface === 'resource' ? Object.fromEntries(rows.map(row => [row.key, value(row)])) : {}) });
  const resourceBefore = JSON.stringify(resource.attributes);
  const provider = register({ projectName: 'genkit-content-contract', projectType: ProjectType.OBSERVE, resource,
    setGlobalTracerProvider: false, endpoint: origin + '/v1/traces', headers: { 'X-Api-Key': 'synthetic', 'X-Secret-Key': 'synthetic' } });
  const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, traceConfig: test.flag === 'off' ? {} : { [test.flag]: true } });
  const snapshot = span => JSON.stringify({ attributes: span.attributes, events: span.events, links: span.links, status: span.status });
  const capture = { onStart() {}, onEnd(span) { native.push({ span, original: snapshot(span) }); }, async forceFlush() {}, async shutdown() {} };
  let active;
  try {
    await enableTelemetry({ spanProcessors: [capture, processor], autoDetectResources: false });
    const ai = genkit({ plugins: [] });
    const model = ai.defineModel({ name: 'offline/content' }, async request => {
      const attributes = { [active.key]: value(active) };
      // Probe children of the real native model span have no model request or
      // response envelope that could overwrite a seeded MIME/model attribute.
      // Test BOTH a model and a generic span: non-model usage is namespaced by
      // the mapper and must not bypass privacy through that additional copy.
      const ctx = test.surface === 'context' ? setAttributes(context.active(), attributes) : context.active();
      context.with(ctx, () => {
        for (const typed of [true, false]) {
          const span = trace.getTracer('genkit-privacy-probe').startSpan('content-probe', {
            attributes: typed ? { 'genkit:type': 'action', 'genkit:metadata:subtype': 'model' } : {},
          });
          if (test.surface === 'span') span.setAttributes(attributes);
          if (test.surface === 'event') span.addEvent('synthetic-content', attributes);
          if (test.surface === 'link') span.addLink({ context: { traceId: '11111111111111111111111111111111', spanId: '2222222222222222', traceFlags: 1 }, attributes });
          span.end();
        }
      });
      return (await fetch(origin + '/model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) })).json();
    });
    // One key per native model span avoids attribute-budget drops masquerading as
    // privacy. Resource attributes share one complete resource (not span limits).
    let generated = 0;
    for (const row of test.surface === 'resource' ? rows.slice(0, 1) : rows) {
      active = row;
      assert.equal((await ai.generate({ model, prompt: 'public prompt' })).text, 'public reply');
      // A growing source-derived inventory must not exceed a provider's queue.
      // Flush bounded batches; still require every request and every span below.
      if (++generated % 128 === 0) { await flushTracing(); await processor.forceFlush(); }
    }
    await flushTracing(); await processor.forceFlush();
    const spans = payloads.flatMap(p => p.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []);
    assert.equal(requests.length, test.surface === 'resource' ? 1 : rows.length);
    assert.equal(new Set(spans.map(s => s.spanId)).size, spans.length);
    assert.equal(spans.length, requests.length * 4);
    const selected = test.surface === 'resource' ? payloads.flatMap(p => p.resourceSpans ?? []).flatMap(r => r.resource.attributes ?? [])
      : ['span', 'context'].includes(test.surface) ? spans.filter(s => s.name === 'content-probe').flatMap(s => s.attributes ?? [])
      : spans.flatMap(s => s[test.surface === 'event' ? 'events' : 'links'] ?? []).flatMap(s => s.attributes ?? []);
    const wire = JSON.stringify(payloads);
    const outcomes = rows.map(row => {
      const hidden = test.flag !== 'off' && row.side !== 'metadata' && (row.side === 'both' || test.flag.toLowerCase().includes(row.side));
      const attrs = selected.filter(a => a.key === row.key);
      const present = wire.includes(row.marker);
      const placeholder = attrs.length === 0 || attrs.every(a => JSON.stringify(a.value).includes('__REDACTED__'));
      return { ...row, flag: test.flag, surface: test.surface, hidden, markerPresent: present, selectedCount: attrs.length,
        passed: hidden ? !present && placeholder : row.defaultMediaOmission
          ? !present && attrs.length > 0 && attrs.every(a => a.value.stringValue === '<omitted: media>')
          : present && attrs.some(a => JSON.stringify(a.value).includes(row.marker)) };
    });
    const evidence = { case: test.name, inventory: rows, requests, payloads, outcomes };
    if (process.env.PRIVACY_EVIDENCE_DIR) writeFileSync(`${process.env.PRIVACY_EVIDENCE_DIR}/${test.name}.json`, JSON.stringify(evidence));
    for (const { span, original } of native) {
      assert.equal(snapshot(span), original, 'native span remains unchanged');
      const exported = spans.find(s => s.spanId === span.spanContext().spanId);
      assert.equal(exported.traceId, span.spanContext().traceId);
      assert.equal(exported.parentSpanId || undefined, span.parentSpanContext?.spanId ?? span.parentSpanId);
    }
    assert.equal(JSON.stringify(resource.attributes), resourceBefore, 'resource remains unchanged');
    const failed = outcomes.filter(row => !row.passed);
    console.log(JSON.stringify({ case: test.name, attributes: rows.length, passed: rows.length - failed.length, failed: failed.length, failures: failed.map(row => ({ key: row.key, side: row.side, hidden: row.hidden, markerPresent: row.markerPresent })) }));
    assert.equal(failed.length, 0, 'source-derived attributes obey processor policy at the complete OTLP receiver');
  } finally { await processor.shutdown(); await new Promise(resolve => server.close(resolve)); }
}
