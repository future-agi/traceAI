const { test } = require('node:test');
const assert = require('node:assert/strict');
const { fixture, spans, attributes, fs, path, child } = require('./helpers.cjs');
const { CursorObserver, normalizeEvent } = require('../dist/index.js');
const { installRuntime, register, commands } = require('../dist/registration.js');

async function printHooks() {
  return JSON.parse(await fs.readFile(path.join(__dirname, 'fixtures/real-1106-cli-print.json'), 'utf8'));
}
for (const usage of [{}, { input_tokens: 0, output_tokens: 0 }, { cache_read_tokens: 0 }]) {
  test(`orphan CLI terminal with no activity is not a trace: ${JSON.stringify(usage)}`, async t => {
    const f = await fixture(t), observer = new CursorObserver(f.config);
    const terminal = (await printHooks()).find(p => p.hook_event_name === 'sessionEnd');
    assert(terminal);
    const e = normalizeEvent({ ...terminal, ...usage }, 'sessionEnd', f.config);
    await observer.accept(e);
    await observer.accept(e);
    await observer.close();
    assert.equal(spans(f.requests).length, 0);
    assert(observer.gaps.has('EMPTY_SESSION'));
  });
}
test('print-mode tool activity survives without a prompt or usage', async t => {
  const f = await fixture(t), observer = new CursorObserver(f.config);
  for (const row of await printHooks()) await observer.accept(normalizeEvent(row, row.hook_event_name, f.config));
  await observer.close();
  const all = spans(f.requests);
  assert.equal(all.filter(s => !s.parentSpanId).length, 1);
  assert(all.some(s => attributes(s)['fi.span.kind'] === 'TOOL'));
  assert(!observer.gaps.has('EMPTY_SESSION'));
});
for (const usage of [{ input_tokens: 10, output_tokens: 4 }, { cache_read_tokens: 5 }]) {
  test(`terminal-only positive usage is retained: ${JSON.stringify(usage)}`, async t => {
    const f = await fixture(t), observer = new CursorObserver(f.config);
    const terminal = (await printHooks()).find(p => p.hook_event_name === 'sessionEnd');
    await observer.accept(normalizeEvent({ ...terminal, ...usage }, 'sessionEnd', f.config));
    await observer.close();
    assert.equal(spans(f.requests).filter(s => !s.parentSpanId).length, 1);
    assert(!observer.gaps.has('EMPTY_SESSION'));
  });
}
test('durable terminal-only hook records EMPTY_SESSION in diagnostics without exporting', async t => {
  const f = await fixture(t);
  const terminal = (await printHooks()).find(p => p.hook_event_name === 'sessionEnd');
  const runtime = await installRuntime(f.context);
  await register(f.context, runtime);
  const hook = commands(runtime, f.config.paths.configDir).sessionEnd.command;
  const result = await child(hook, { ...terminal, workspace_roots: [f.home], transcript_path: null }, { HOME: f.home, PATH: '/missing' });
  assert.equal(result.code, 0);
  assert.equal(result.stderr, '');
  assert.equal(spans(f.requests).length, 0);
  const diagnostics = JSON.parse(await fs.readFile(path.join(f.config.paths.stateDir, 'cursor-diagnostics.json'), 'utf8'));
  assert(diagnostics.gaps.includes('EMPTY_SESSION'));
  const status = await require('../dist/state.js').finisherStatus(f.config);
  assert.equal(status.last.result, 'EMPTY_SESSION');
});
