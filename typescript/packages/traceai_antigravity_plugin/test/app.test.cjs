const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn} = require('node:child_process');
const core = require('@traceai/agent-core');
const {surface, applyEvent} = require('../dist/mapper');
const {enrichTranscript} = require('../dist/transcript');
const {antigravityAdapter: adapter, PLUGIN_NAME} = require(process.env.AGY_PACKAGE_PATH || '../dist');
const attrs = s => Object.fromEntries(s.attributes.map(a => [a.key, Object.values(a.value)[0]]));
const fixtures = path.join(__dirname, 'fixtures');
const rows = async name => (await fs.readFile(path.join(fixtures, name), 'utf8')).trim().split('\n').map(JSON.parse);
function run(exe, args, env, input, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(exe, args, {env, cwd, stdio: ['pipe', 'pipe', 'pipe']});
    let stdout = '', stderr = '';
    child.stdout.on('data', b => stdout += b); child.stderr.on('data', b => stderr += b);
    child.on('error', reject); child.on('close', code => resolve({code, stdout, stderr}));
    child.stdin.on('error', () => {}); child.stdin.end(input);
  });
}
async function homeFixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-agy-app-'));
  t.after(() => fs.rm(home, {recursive: true, force: true}));
  return home;
}

test('observed app and CLI paths are accepted; IDE, mixed, relative and traversal paths stay gated', () => {
  const cli = '/Users/fixture/.gemini/antigravity-cli/brain/test';
  const app = '/Users/fixture/.gemini/antigravity/brain/test';
  assert.equal(surface({artifactDirectoryPath: cli}), 'cli');
  assert.equal(surface({artifactDirectoryPath: app, transcriptPath: app + '/.system_generated/logs/transcript_full.jsonl'}), 'app');
  for (const payload of [
    {}, {transcriptPath: '/Users/fixture/.gemini/antigravity-ide/brain/test/transcript.jsonl'},
    {transcriptPath: '/tmp/transcript.jsonl'}, {transcriptPath: '~/.gemini/antigravity/brain/test'},
    {transcriptPath: app + '/../elsewhere'}, {transcriptPath: app + '/./file'},
    {transcriptPath: app + '\u0000'}, {transcriptPath: app, artifactDirectoryPath: cli},
    {transcriptPath: app, artifactDirectoryPath: '/tmp/elsewhere'},
    {transcriptPath: app, artifactDirectoryPath: null},
    {transcriptPath: 'C:\\Users\\fixture\\.gemini\\antigravity\\brain\\test'},
  ]) assert.equal(surface(payload), undefined, JSON.stringify(payload));
});

for (const filename of ['transcript_full.jsonl', 'transcript.jsonl']) {
  test(`app ${filename} uses the exact-home reader and preserves symlink/hardlink safety`, async t => {
    const home = await homeFixture(t), conversationId = 'test';
    const transcriptPath = path.join(home, '.gemini/antigravity/brain/test/.system_generated/logs', filename);
    await fs.mkdir(path.dirname(transcriptPath), {recursive: true, mode: 0o700});
    const config = {captureContent: true, paths: {user: path.join(home, '.gemini/config/futureagi.json')}};
    const payload = {conversationId, transcriptPath, initialNumSteps: 1, invocationNum: 0, lastUserInput: 'test'};
    const state = () => ({schemaVersion: 1, generation: 0, touched: Date.now(), completed: []});
    const contents = (await rows('real-app-2.28.2-transcript.jsonl')).slice(0, 3).map(r => JSON.stringify(r)).join('\n') + '\n';
    await fs.writeFile(transcriptPath, contents);
    const valid = state(); applyEvent(valid, 'PreInvocation', payload, config);
    await enrichTranscript(valid, [valid.active], payload, config);
    assert.equal(valid.active.surface, 'app');
    assert.equal(valid.active.activities.filter(a => a.kind === 'llm').length, 1);
    const foreignHome = await homeFixture(t);
    const foreign = path.join(foreignHome, '.gemini/antigravity/brain/test/.system_generated/logs', filename);
    await fs.mkdir(path.dirname(foreign), {recursive: true, mode: 0o700}); await fs.writeFile(foreign, contents);
    for (const invalid of [
      {...payload, transcriptPath: foreign}, {...payload, conversationId: '../test'},
      {...payload, conversationId: 'other'}, {...payload, transcriptPath: transcriptPath + '.other'},
      {...payload, transcriptPath: transcriptPath.replace('/antigravity/', '/antigravity-ide/')},
    ]) {
      const s = state(); assert.equal(await enrichTranscript(s, [], invalid, config), false); assert.equal(s.transcript, undefined);
    }
    await fs.unlink(transcriptPath); await fs.symlink(foreign, transcriptPath);
    await assert.rejects(enrichTranscript(state(), [], payload, config));
    await fs.unlink(transcriptPath); await fs.link(foreign, transcriptPath);
    await assert.rejects(enrichTranscript(state(), [], payload, config), /UNSAFE_TRANSCRIPT/);
    await fs.unlink(transcriptPath); await fs.writeFile(transcriptPath, contents);
    await fs.chmod(path.dirname(transcriptPath), 0o777);
    await assert.rejects(enrichTranscript(state(), [], payload, config));
  });
}

test('detect accepts macOS agy 1.4.2 and still rejects Windows before invoking agy', async t => {
  const home = await homeFixture(t), bin = path.join(home, 'bin'); await fs.mkdir(bin);
  await fs.writeFile(path.join(bin, 'agy'), '#!/bin/sh\nprintf "1.4.2\\n"\n', {mode: 0o700});
  const entry = path.resolve(__dirname, '../dist/adapter.js');
  for (const platform of ['darwin', 'linux', 'win32']) {
    const script = `Object.defineProperty(process,'platform',{value:${JSON.stringify(platform)}});require(${JSON.stringify(entry)}).antigravityAdapter.detect({config:{paths:{user:${JSON.stringify(path.join(home, '.gemini/config/futureagi.json'))}}}}).then(x=>console.log(JSON.stringify(x)));`;
    const result = await run(process.execPath, ['-e', script], {PATH: platform === 'win32' ? '' : bin, HOME: home});
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), platform === 'win32'
      ? {supported: false, reason: 'UNSUPPORTED_PLATFORM'} : {supported: true, version: '1.4.2'});
    assert.ok(!result.stdout.includes('LINUX_CLI_ONLY'));
  }
});

for (const kind of ['empty', 'nonempty', 'symlink', 'unsafe-parent', 'unowned']) {
  test(`uninstall preserves user content and only removes owned empty app data (${kind})`, async t => {
    const home = await homeFixture(t), configDir = path.join(home, '.config/futureagi/agents');
    await fs.mkdir(configDir, {recursive: true, mode: 0o700});
    const config = {paths: {user: path.join(home, '.gemini/config/futureagi.json'), configDir}};
    const data = path.join(home, '.gemini/antigravity/plugin_data', PLUGIN_NAME);
    await fs.mkdir(path.dirname(data), {recursive: true, mode: 0o700});
    if (kind === 'symlink') { const target = path.join(home, 'owned-by-user'); await fs.mkdir(target); await fs.symlink(target, data); }
    else await fs.mkdir(data, {mode: 0o700});
    if (kind === 'nonempty') await fs.writeFile(path.join(data, 'keep'), 'user content');
    if (kind === 'unsafe-parent') await fs.chmod(path.dirname(data), 0o777);
    if (kind !== 'unowned') await core.writeProtected(path.join(configDir, 'antigravity-registration.json'), JSON.stringify({schemaVersion: 1, hashes: {}}));
    const bin = path.join(home, 'bin'); await fs.mkdir(bin);
    await fs.writeFile(path.join(bin, 'agy'), '#!/bin/sh\nprintf "[]\\n"\n', {mode: 0o700});
    const old = process.env.PATH;
    try {
      process.env.PATH = bin;
      assert.equal((await adapter.uninstall({config, scope: 'user'})).registered, false);
    } finally { process.env.PATH = old; }
    if (kind === 'empty') await assert.rejects(fs.lstat(data), {code: 'ENOENT'});
    else assert.ok(await fs.lstat(data));
    if (kind === 'nonempty') assert.equal(await fs.readFile(path.join(data, 'keep'), 'utf8'), 'user content');
  });
}

test('real macOS App 2.28.2 golden replay through the agy-installed hook; no workspace; duplicate Stop is idempotent',
  {skip: process.env.AGY_TEST_REAL !== '1'}, async t => {
    const home = await homeFixture(t), exports = [];
    const server = http.createServer((req, res) => {
      const chunks = []; req.on('data', b => chunks.push(b)); req.on('end', () => {
        assert.equal(req.headers['x-api-key'], 'synthetic-api'); assert.equal(req.headers['x-secret-key'], 'synthetic-secret');
        exports.push(JSON.parse(Buffer.concat(chunks).toString()));
        res.setHeader('Content-Type', 'application/json'); res.end('{}');
      });
    });
    await new Promise(r => server.listen(0, '127.0.0.1', r));
    t.after(async () => {server.closeAllConnections(); await new Promise(r => server.close(r));});
    const options = {agent: 'antigravity', home, env: {}};
    const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
    let setup;
    if (process.env.AGY_CLI_PATH) {
      const result = await run(process.execPath, [process.env.AGY_CLI_PATH, 'setup', 'antigravity', '--project', 'app-golden', '--json'],
        {HOME: home, PATH: process.env.PATH, FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret', FI_OTLP_ENDPOINT: endpoint}, '', home);
      assert.equal(result.code, 0, result.stdout + result.stderr);
      setup = JSON.parse(result.stdout);
    } else {
      setup = await core.setup({...options, projectName: 'app-golden',
        credentials: {apiKey: 'synthetic-api', secretKey: 'synthetic-secret'}, otlpEndpoint: endpoint, adapter});
    }
    assert.equal(setup.registered, true, JSON.stringify(setup));
    const config = await core.resolveConfig(options);
    const installed = path.join(home, '.gemini/config/plugins', PLUGIN_NAME);
    const hooks = JSON.parse(await fs.readFile(path.join(installed, 'hooks.json'), 'utf8'))[PLUGIN_NAME];
    // Fixtures retain every real field; only the sanitized home is rebased into this sandbox.
    const events = JSON.parse(JSON.stringify(await rows('real-app-2.28.2.jsonl')).replaceAll('/Users/fixture', home));
    const transcript = await rows('real-app-2.28.2-transcript.jsonl');
    const transcriptPath = events[0].payload.transcriptPath;
    await fs.mkdir(path.dirname(transcriptPath), {recursive: true, mode: 0o700});
    const send = async ({event, payload}) => {
      const handler = event === 'PostToolUse' ? hooks[event][0].hooks[0] : hooks[event][0];
      const result = await run('/bin/sh', ['-c', handler.command], {HOME: home, PATH: '/usr/bin:/bin'}, JSON.stringify(payload), installed);
      assert.equal(result.code, 0); assert.equal(result.stderr, '');
      assert.equal(result.stdout, event === 'Stop' ? '{"decision":""}\n' : '{}\n');
    };
    for (const [i, record] of events.entries()) {
      assert.deepEqual(record.payload.workspacePaths, []);
      const available = i < 2 ? 1 : i < 5 ? 3 : 4;
      await fs.writeFile(transcriptPath, transcript.slice(0, available).map(r => JSON.stringify(r)).join('\n') + '\n');
      await send(record);
    }
    await send(events.at(-1));
    const spans = exports.flatMap(r => r.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(s => s.spans)));
    const roots = spans.filter(s => !s.parentSpanId), generations = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
    const invocations = spans.filter(s => s.name === 'invocation'), tools = spans.filter(s => attrs(s)['fi.span.kind'] === 'TOOL');
    assert.equal(spans.length, 6); assert.equal(roots.length, 1); assert.equal(invocations.length, 2);
    assert.equal(generations.length, 2); assert.equal(tools.length, 1); assert.equal(tools[0].name, 'run_command');
    assert.equal(attrs(roots[0])['fi.agent.surface'], 'app');
    assert.equal(attrs(roots[0])['input.value'], events[0].payload.lastUserInput);
    assert.equal(attrs(roots[0])['output.value'], 'The result is `42`.');
    assert.ok(attrs(tools[0])['input.value'].includes('print(6*7)')); assert.ok(attrs(tools[0])['output.value'].includes('42'));
    assert.ok(spans.every(s => attrs(s)['fi.agent.outcome'] === 'success'));
    assert.ok(invocations.every(s => s.parentSpanId === roots[0].spanId));
    assert.ok(generations.every(s => invocations.some(p => p.spanId === s.parentSpanId)));
    assert.ok(spans.every(s => !s.parentSpanId || spans.some(p => p.spanId === s.parentSpanId && p.traceId === s.traceId)));
    assert.deepEqual(invocations.map(s => generations.filter(g => g.parentSpanId === s.spanId).length), [1, 1]);
    assert.equal(tools[0].parentSpanId, invocations[0].spanId);
    const sum = key => spans.reduce((n, s) => n + Number(attrs(s)[key] ?? 0), 0);
    // 12,345 uncached + 8,092 cache reads, inclusive once on generations only.
    assert.equal(sum('gen_ai.usage.input_tokens'), 20437);
    assert.equal(sum('gen_ai.usage.total_tokens'), 20501);
    assert.equal(sum('gen_ai.usage.output_tokens'), 64);
    assert.equal(sum('gen_ai.usage.cache_read_tokens'), 8092);
    assert.ok([...roots, ...invocations, ...tools].every(s => attrs(s)['gen_ai.usage.total_tokens'] === undefined));
    assert.equal((await core.inspectStatus(options)).delivery.exported, 6);
    const tree = s => ({name: s.name, kind: attrs(s)['fi.span.kind'], outcome: attrs(s)['fi.agent.outcome'],
      surface: attrs(s)['fi.agent.surface'], inputTokens: attrs(s)['gen_ai.usage.input_tokens'], outputTokens: attrs(s)['gen_ai.usage.output_tokens'],
      cacheReadTokens: attrs(s)['gen_ai.usage.cache_read_tokens'],
      children: spans.filter(c => c.parentSpanId === s.spanId).map(tree)});
    const receipt = {source: 'macOS App 2.28.2 / agy 1.4.2', replay: 'E2B Linux agy-installed hook with loopback OTLP', roots: roots.length,
      spans: spans.length, inputTokens: sum('gen_ai.usage.input_tokens'), outputTokens: sum('gen_ai.usage.output_tokens'), tree: tree(roots[0])};
    console.log('APP_GOLDEN_OTLP', JSON.stringify(receipt));
    if (process.env.AGY_EVIDENCE_DIR) {
      await fs.mkdir(process.env.AGY_EVIDENCE_DIR, {recursive: true});
      await fs.writeFile(path.join(process.env.AGY_EVIDENCE_DIR, 'app-golden.json'), JSON.stringify({receipt, exports}, null, 2));
    }
    // A second native prompt in the same conversation gets its own root, not a merged turn.
    const again = JSON.parse(JSON.stringify(events));
    const resumed = transcript.map(row => ({...row, step_index: row.step_index + 4}));
    for (const [i, record] of again.entries()) {
      if (record.payload.initialNumSteps !== undefined) record.payload.initialNumSteps += 4;
      if (record.payload.stepIdx !== undefined) record.payload.stepIdx += 4;
      const available = i < 2 ? 1 : i < 5 ? 3 : 4;
      await fs.writeFile(transcriptPath, [...transcript, ...resumed.slice(0, available)].map(r => JSON.stringify(r)).join('\n') + '\n');
      await send(record);
    }
    const all = exports.flatMap(r => r.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(s => s.spans)));
    const allRoots = all.filter(s => !s.parentSpanId);
    assert.equal(allRoots.length, 2); assert.equal(all.length, 12);
    assert.equal(attrs(allRoots[0])['session.id'], attrs(allRoots[1])['session.id']);
    assert.notEqual(allRoots[0].traceId, allRoots[1].traceId);
    const data = path.join(home, '.gemini/antigravity/plugin_data', PLUGIN_NAME);
    await fs.mkdir(data, {recursive: true, mode: 0o700});
    assert.equal((await adapter.uninstall({config, scope: 'user'})).registered, false);
    await assert.rejects(fs.lstat(data), {code: 'ENOENT'});
  });
