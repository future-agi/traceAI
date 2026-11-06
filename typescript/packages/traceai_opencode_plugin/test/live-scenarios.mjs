import { OpenCode } from '@opencode/client';
import { spawn } from 'node:child_process';
import { writeFile, readFile, mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';

export async function scenarios({ binary, env, project, captures, models, run }) {
  const url = 'http://127.0.0.1:43107';
  env.OPENCODE_PASSWORD = 'synthetic-local-test-only';
  let serverLog = '';
  function startServer() {
    const process = spawn(binary, ['serve', '--hostname', '127.0.0.1', '--port', '43107'], { cwd: project, env, stdio: ['ignore', 'pipe', 'pipe'] });
    process.stdout.on('data', b => serverLog += b); process.stderr.on('data', b => serverLog += b);
    return process;
  }
  let child = startServer();
  const client = OpenCode.make({ baseUrl: url, headers: { Authorization: `Basic ${Buffer.from(`opencode:${env.OPENCODE_PASSWORD}`).toString('base64')}` } });
  const pause = ms => new Promise(r => setTimeout(r, ms));
  const all = () => captures.flatMap(r => r.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(s => s.spans)));
  const attrs = s => Object.fromEntries(s.attributes.map(a => [a.key, Object.values(a.value)[0]]));
  const root = s => !s.parentSpanId;
  async function prompt(sessionID, text) {
    const admitted = await client.session.prompt({ sessionID, text });
    await client.experimental.session.wait({ sessionID }, { signal: AbortSignal.timeout(30_000) });
    // Core batches finalized spans on a one-second timer when another flush is in flight.
    await pause(1250);
    return admitted;
  }
  async function ready() {
    for (let i = 0; ; i++) {
      try { await client.server.info({ signal: AbortSignal.timeout(1000) }); return; }
      catch { if (i > 100) throw new Error('SERVER_START_TIMEOUT'); await pause(100); }
    }
  }
  async function stopServer() {
    const exited = new Promise(r => child.once('exit', r));
    child.kill('SIGTERM');
    await Promise.race([exited, pause(3000)]);
    if (child.exitCode === null && child.signalCode === null) { child.kill('SIGKILL'); await exited; }
  }
  const nativeEvents = async () => (await readFile('/work/evidence/events.jsonl', 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  function assertCompleteTree(spans, events, sessionIDs) {
    const local = events.filter(e => sessionIDs.includes(e.data?.sessionID));
    const generations = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
    const steps = local.filter(e => e.type === 'session.step.ended');
    const tools = local.filter(e => e.type === 'session.tool.success' || e.type === 'session.tool.failed');
    assert.equal(spans.filter(root).length, 1, 'no synthetic second user root');
    assert.equal(new Set(spans.map(s => s.traceId)).size, 1, 'all subagent work shares its parent trace');
    assert.equal(generations.length, steps.length, 'every native completed model request is represented');
    assert.equal(spans.filter(s => attrs(s)['fi.span.kind'] === 'TOOL').length, tools.length, 'every completed tool is represented');
    const total = steps.reduce((sum, e) => sum + e.data.tokens.input + e.data.tokens.output + e.data.tokens.reasoning + e.data.tokens.cache.read + e.data.tokens.cache.write, 0);
    assert.equal(generations.reduce((sum, s) => sum + Number(attrs(s)['gen_ai.usage.total_tokens']), 0), total, 'all native token usage retained exactly once');
    const final = local.filter(e => e.type === 'session.execution.succeeded' && e.data.sessionID === sessionIDs[0]).at(-1);
    assert.ok(BigInt(spans.find(root).endTimeUnixNano) >= BigInt(final.created) * 1000000n, 'root includes the parent continuation');
    assert.equal(attrs(spans.find(root))['output.value'], 'The sample file contains public text.');
    return { nativeRequests: steps.length, nativeTools: tools.length, totalTokens: total, roots: 1 };
  }
  const tokenTotal = tokens => tokens ? tokens.input + tokens.output + tokens.reasoning + tokens.cache.read + tokens.cache.write : 0;
  const nativeUsage = (events, sessionID) => tokenTotal(events.filter(e => e.type === 'session.usage.updated' && e.data.sessionID === sessionID).at(-1)?.data.tokens);
  async function assertUsageDelta(spans, sessionID, previous = []) {
    const native = nativeUsage(await nativeEvents(), sessionID) - nativeUsage(previous, sessionID);
    const exported = spans.filter(s => attrs(s)['fi.agent.usage.additive'] === true)
      .reduce((sum, s) => sum + Number(attrs(s)['gen_ai.usage.total_tokens'] ?? 0), 0);
    assert.equal(exported, native, 'additive exported usage equals native session-usage delta');
    return { native, exported };
  }
  const results = {};
  try {
    await ready();
    const location = { directory: project };
    const plugins = await client.plugin.list({ location });
    results.plugins = plugins;
    // V2 lists only activated locations; a fresh server has no active plugin inventory yet.
    const create = (title, extra = {}) => client.session.create({ location, title, model: { id: 'test', providerID: 'mock' }, ...extra });
    const session = await create('Two prompt session');
    const before = all().length;
    await prompt(session.id, '[first] Read sample.txt');
    await prompt(session.id, '[second] Explain it again');
    const turns = all().slice(before).filter(root);
    assert.equal(turns.length, 2, 'two roots for two prompts');
    assert.equal(attrs(turns[0])['session.id'], attrs(turns[1])['session.id']);
    assert.notEqual(turns[0].traceId, turns[1].traceId);
    results.twoPrompts = turns.map(s => s.traceId);
    results.pluginsAfterPrompt = await client.plugin.list({ location });

    const c1 = await create('Concurrent one'), c2 = await create('Concurrent two');
    const concurrentBefore = all().length;
    await Promise.all([prompt(c1.id, '[concurrent-one] Read sample.txt'), prompt(c2.id, '[concurrent-two] Read sample.txt')]);
    const concurrent = all().slice(concurrentBefore).filter(root);
    assert.equal(concurrent.length, 2);
    assert.notEqual(attrs(concurrent[0])['session.id'], attrs(concurrent[1])['session.id']);
    results.concurrent = true;

    const retry = await create('Retry model request');
    const retryBefore = all().length;
    await prompt(retry.id, '[retry] Read sample.txt');
    const retried = all().slice(retryBefore);
    results.retry = retried.map(s => ({ name: s.name, kind: attrs(s)['fi.span.kind'], status: s.status }));
    assert.ok(retried.some(s => attrs(s)['fi.span.kind'] === 'LLM' && s.status.code === 2), 'failed model attempt observed');
    assert.equal(attrs(retried.find(root))['fi.agent.outcome'], 'success', 'successful retry recovers the root');
    results.retryUsage = await assertUsageDelta(retried, retry.id);

    const terminal = await create('Terminal model authentication error');
    const terminalBefore = all().length;
    await prompt(terminal.id, '[terminal-auth-error] Read sample.txt');
    const terminalSpans = all().slice(terminalBefore);
    assert.equal(attrs(terminalSpans.find(root))['fi.agent.outcome'], 'error');
    assert.equal(terminalSpans.find(root).status.code, 2);
    results.terminalModelFailure = true;

    const failure = await create('Failed tool');
    const failureBefore = all().length;
    await prompt(failure.id, '[tool-failure] Read a missing file');
    const failed = all().slice(failureBefore);
    assert.ok(failed.some(s => attrs(s)['fi.span.kind'] === 'TOOL' && s.status.code === 2));
    results.toolFailure = true;

    const nested = await create('Nested child');
    const nestedBefore = all().length;
    await prompt(nested.id, '[subagent] Delegate reading sample.txt');
    const nestedSpans = all().slice(nestedBefore);
    results.subagent = nestedSpans.map(s => ({ name: s.name, kind: attrs(s)['fi.span.kind'], traceId: s.traceId, parentSpanId: s.parentSpanId }));
    assert.equal(nestedSpans.filter(root).length, 1, 'child joins the user turn, not a new root');
    const branch = nestedSpans.find(s => s.name === 'subagent' && attrs(s)['fi.span.kind'] === 'AGENT');
    const spawningTool = nestedSpans.find(s => s.name === 'subagent' && attrs(s)['fi.span.kind'] === 'TOOL');
    assert.ok(branch && spawningTool);
    assert.equal(branch.parentSpanId, spawningTool.spanId, 'source tool metadata proves child ancestry');


    const bg = await create('Background child');
    const bgBefore = all().length;
    await prompt(bg.id, '[background-subagent] Delegate and tell me when done');
    // Child deliberately takes five seconds, so the first parent execution ends first.
    for (let i = 0; ; i++) {
      const events = await nativeEvents();
      if (events.filter(e => e.type === 'session.execution.succeeded' && e.data.sessionID === bg.id).length === 2) break;
      if (i > 150) throw new Error('BACKGROUND_CONTINUATION_TIMEOUT');
      await pause(100);
    }
    await pause(1500);
    const bgEvents = await nativeEvents();
    const bgChild = bgEvents.find(e => e.type === 'session.created' && e.data.parentID === bg.id).data.sessionID;
    results.background = assertCompleteTree(all().slice(bgBefore), bgEvents, [bg.id, bgChild]);
    assert.equal(results.background.nativeRequests, 6);
    assert.equal(results.background.totalTokens, 168);

    const followed = await create('User prompt while background task continues');
    const followedBefore = all().length;
    await prompt(followed.id, '[background-subagent] Delegate and tell me when done');
    const firstEvents = await nativeEvents();
    const followedChild = firstEvents.find(e => e.type === 'session.created' && e.data.parentID === followed.id).data.sessionID;
    assert.ok(!firstEvents.some(e => e.type === 'session.execution.succeeded' && e.data.sessionID === followedChild), 'child must still be running when the intervening prompt is submitted');
    await prompt(followed.id, '[intervening-user] Read sample.txt again');
    for (let i = 0; ; i++) {
      if ((await nativeEvents()).filter(e => e.type === 'session.execution.succeeded' && e.data.sessionID === followed.id).length === 3) break;
      if (i > 150) throw new Error('INTERVENING_CONTINUATION_TIMEOUT');
      await pause(100);
    }
    await pause(1500);
    const followedSpans = all().slice(followedBefore), followedEvents = (await nativeEvents()).filter(e => [followed.id, followedChild].includes(e.data?.sessionID));
    const followedRoots = followedSpans.filter(root);
    assert.equal(followedRoots.length, 2);
    assert.equal(new Set(followedRoots.map(s => s.traceId)).size, 2);
    assert.ok(followedRoots.every(s => attrs(s)['fi.agent.outcome'] === 'success' && s.status.code === 1));
    assert.ok(!followedEvents.some(e => ['session.execution.failed', 'session.execution.interrupted'].includes(e.type)));
    const original = followedRoots.find(s => attrs(s)['input.value'].includes('[background-subagent]'));
    const next = followedRoots.find(s => attrs(s)['input.value'].includes('[intervening-user]'));
    const firstTree = followedSpans.filter(s => s.traceId === original.traceId);
    assert.equal(firstTree.filter(s => attrs(s)['fi.span.kind'] === 'LLM').length, 6);
    assert.equal(followedSpans.filter(s => s.traceId === next.traceId && attrs(s)['fi.span.kind'] === 'LLM').length, 2);
    const nativeGenerations = followedEvents.filter(e => e.type === 'session.step.ended').length;
    const nativeTools = followedEvents.filter(e => ['session.tool.success', 'session.tool.failed'].includes(e.type)).length;
    assert.equal(nativeGenerations, 8); assert.equal(nativeTools, 4);
    assert.equal(followedSpans.filter(s => attrs(s)['fi.span.kind'] === 'LLM').length, nativeGenerations);
    assert.equal(followedSpans.filter(s => attrs(s)['fi.span.kind'] === 'TOOL').length, nativeTools);
    const followedTotal = followedSpans.reduce((sum, s) => sum + Number(attrs(s)['gen_ai.usage.total_tokens'] ?? 0), 0);
    assert.equal(followedTotal, 224);
    assert.equal(followedTotal, nativeUsage(followedEvents, followed.id) + nativeUsage(followedEvents, followedChild));
    const finalContinuation = followedEvents.filter(e => e.type === 'session.execution.succeeded' && e.data.sessionID === followed.id).at(-1);
    assert.ok(BigInt(original.endTimeUnixNano) >= BigInt(finalContinuation.created) * 1000000n);
    assert.equal(attrs(original)['output.value'], 'The sample file contains public text.');
    results.backgroundFollow = { roots: 2, nativeGenerations, nativeTools, totalTokens: followedTotal, originalGenerations: 6, interveningGenerations: 2, outcomes: followedRoots.map(s => attrs(s)['fi.agent.outcome']) };

    const dynamic = await create('Foreground child moved to background');
    const dynamicStart = all().length;
    await client.session.prompt({ sessionID: dynamic.id, text: '[foreground-to-background] Delegate reading sample.txt' });
    let dynamicChild;
    for (let i = 0; ; i++) {
      const events = await nativeEvents();
      dynamicChild = events.find(e => e.type === 'session.created' && e.data.parentID === dynamic.id)?.data.sessionID;
      if (dynamicChild && events.some(e => e.type === 'session.execution.started' && e.data.sessionID === dynamicChild)) break;
      if (i > 100) throw new Error('DYNAMIC_CHILD_TIMEOUT');
      await pause(50);
    }
    await pause(350);
    await client.session.background({ sessionID: dynamic.id });
    for (let i = 0; ; i++) {
      const events = await nativeEvents();
      if (events.filter(e => e.type === 'session.execution.succeeded' && e.data.sessionID === dynamic.id).length >= 2) break;
      if (i > 200) throw new Error('DYNAMIC_CONTINUATION_TIMEOUT');
      await pause(100);
    }
    await pause(1500);
    results.dynamicBackground = assertCompleteTree(all().slice(dynamicStart), await nativeEvents(), [dynamic.id, dynamicChild]);
    assert.equal(results.dynamicBackground.nativeRequests, 7);
    assert.equal(results.dynamicBackground.nativeTools, 4);
    assert.equal(results.dynamicBackground.totalTokens, 196);

    // Regression: multiple steering prompts and notifications can participate
    // in one native execution. Every root must retain the shared final answer.
    async function assertSharedExecution(name, session, before) {
      const spans = all().slice(before), events = await nativeEvents();
      const children = events.filter(e => e.type === 'session.created' && e.data.parentID === session.id).map(e => e.data.sessionID);
      const local = events.filter(e => [session.id, ...children].includes(e.data?.sessionID));
      const roots = spans.filter(root);
      assert.equal(roots.length, 3, `${name}: one root for each user prompt`);
      for (const r of roots) {
        assert.equal(attrs(r)['output.value'], 'The sample file contains public text.', `${name}: every participating prompt has its final response`);
        assert.equal(attrs(r)['fi.agent.outcome'], 'success');
      }
      const links = roots.flatMap(r => r.events ?? []).filter(e => e.name.startsWith('opencode.shared-generation.'));
      assert.ok(links.length >= 2, `${name}: explicit source span linkage`);
      for (const link of links) {
        const [traceId, spanId] = link.name.slice('opencode.shared-generation.'.length).split('.');
        assert.ok(spans.some(s => s.traceId === traceId && s.spanId === spanId && attrs(s)['fi.span.kind'] === 'LLM'));
      }
      const steps = local.filter(e => e.type === 'session.step.ended');
      assert.equal(spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM').length, steps.length);
      const expected = steps.reduce((sum, e) => sum + tokenTotal(e.data.tokens), 0);
      assert.equal(spans.reduce((sum, s) => sum + Number(attrs(s)['gen_ai.usage.total_tokens'] ?? 0), 0), expected);
      results[name] = { roots: roots.length, generations: steps.length, sharedLinks: links.length, totalTokens: expected };
    }
    const queued = await create('Co-delivered steering prompts');
    const queuedBefore = all().length;
    await client.session.prompt({ sessionID: queued.id, text: '[slow-child] [queue-first] Read sample.txt' });
    await pause(400);
    await client.session.prompt({ sessionID: queued.id, text: '[queue-second-unique] Read sample.txt' });
    await client.session.prompt({ sessionID: queued.id, text: '[queue-third-unique] Read sample.txt' });
    await client.experimental.session.wait({ sessionID: queued.id }, { signal: AbortSignal.timeout(30_000) });
    await pause(1800);
    await assertSharedExecution('coDeliveredSteering', queued, queuedBefore);

    const many = await create('Background notifications during another prompt');
    const manyBefore = all().length;
    await prompt(many.id, '[background-subagent] [bg-first] Delegate and tell me when done');
    await prompt(many.id, '[background-subagent] [bg-second] Delegate and tell me when done');
    await prompt(many.id, '[slow-child] [bg-intervening] Read sample.txt independently');
    await pause(12000);
    await client.experimental.session.wait({ sessionID: many.id }, { signal: AbortSignal.timeout(30_000) });
    await pause(1500);
    await assertSharedExecution('overlappingBackground', many, manyBefore);

    // Existing child sessions emit no new creation event, across reload AND process restart.
    const childID = (await nativeEvents()).find(e => e.type === 'session.created' && e.data.parentID === nested.id).data.sessionID;
    for (const restart of [false, true]) {
      if (restart) { await stopServer(); child = startServer(); await ready(); }
      else await client.location.reload();
      const beforeEvents = (await nativeEvents()).length, beforeSpans = all().length;
      await prompt(nested.id, `[resume-subagent:${childID}] Continue the earlier child conversation.`);
      const events = (await nativeEvents()).slice(beforeEvents), spans = all().slice(beforeSpans);
      assert.equal(events.filter(e => e.type === 'session.created' && e.data.sessionID === childID).length, 0);
      const result = assertCompleteTree(spans, events, [nested.id, childID]);
      const agent = spans.find(s => s.name === 'subagent' && attrs(s)['fi.span.kind'] === 'AGENT');
      const tool = spans.find(s => s.name === 'subagent' && attrs(s)['fi.span.kind'] === 'TOOL');
      assert.ok(agent && tool, 'resumed child remains a nested agent');
      assert.equal(agent.parentSpanId, tool.spanId);
      results[restart ? 'resumeAfterRestart' : 'resumeAfterReload'] = result;
    }

    // Manual compaction has no user prompt. It must not invent another root.
    const compactBefore = all().filter(root).length;
    await client.session.compact({ sessionID: session.id });
    await client.experimental.session.wait({ sessionID: session.id }, { signal: AbortSignal.timeout(30_000) });
    await pause(150);
    assert.equal(all().filter(root).length, compactBefore);
    results.manualCompaction = true;

    const automatic = await create('Automatic compaction');
    const automaticBefore = all().length;
    await prompt(automatic.id, '[auto-compact] Read sample.txt');
    const compactSpans = all().slice(automaticBefore);
    results.autoCompaction = compactSpans.map(s => ({ name: s.name, event: attrs(s)['fi.agent.event'] }));
    assert.equal(compactSpans.filter(root).length, 1);
    const compactionSpan = compactSpans.find(s => attrs(s)['fi.agent.event'] === 'compaction');
    assert.ok(compactionSpan, 'auto-compaction is inside the prompt trace');
    assert.equal(compactionSpan.status.code, 1);
    assert.equal(attrs(compactionSpan)['fi.agent.usage.additive'], true);
    assert.equal(attrs(compactionSpan)['fi.agent.fidelity'], 'aggregate-turn');
    assert.equal(attrs(compactionSpan)['fi.span.kind'], 'AGENT');
    assert.equal(Number(attrs(compactionSpan)['gen_ai.usage.total_tokens']), 28);
    assert.equal(attrs(compactSpans.find(root))['fi.agent.outcome'], 'success');
    results.autoCompactionUsage = await assertUsageDelta(compactSpans, automatic.id);
    assert.equal(results.autoCompactionUsage.exported, 100090);

    const summaryRetry = await create('Automatic compaction template retry');
    const summaryBefore = all().length, summaryModels = models.length;
    await prompt(summaryRetry.id, '[auto-compact] [summary-retry] Read sample.txt');
    const summarySpans = all().slice(summaryBefore), summaries = models.slice(summaryModels).filter(m => m.summary);
    assert.equal(summaries.length, 2, 'real native template retry makes two summary requests');
    assert.ok(summaries.some(m => m.templateRetry));
    const aggregate = summarySpans.filter(s => attrs(s)['fi.agent.event'] === 'compaction');
    assert.equal(aggregate.length, 1, 'retry stays an aggregate, not invented request spans');
    assert.equal(Number(attrs(aggregate[0])['gen_ai.usage.total_tokens']), 56);
    assert.equal(attrs(aggregate[0])['fi.span.kind'], 'AGENT');
    results.compactionRetryUsage = await assertUsageDelta(summarySpans, summaryRetry.id);
    assert.equal(results.compactionRetryUsage.exported, 100118);

    const interrupted = await create('Interrupted turn');
    const interruptBefore = all().length;
    await client.session.prompt({ sessionID: interrupted.id, text: '[interrupt] Read sample.txt' });
    await pause(300);
    await client.session.interrupt({ sessionID: interrupted.id });
    await client.experimental.session.wait({ sessionID: interrupted.id }, { signal: AbortSignal.timeout(30_000) });
    await pause(150);
    const interruptedRoot = all().slice(interruptBefore).find(root);
    assert.ok(interruptedRoot);
    assert.equal(attrs(interruptedRoot)['fi.agent.outcome'], 'interrupted');
    results.interrupt = true;

    const overflow = await create('Context overflow recovery');
    // Compare every successful generation's native context snapshot with its real
    // model request, not merely whether the turn's prompt appears somewhere.
    function requestShape(messages, native) {
      return messages.filter(m => m.role !== 'system').map(m => {
        const parts = native ? m.content : [];
        const text = native ? parts.map(p => p.type === 'text' ? p.text : p.type === 'tool-result' ? p.result.value : '').join('') : (typeof m.content === 'string' ? m.content : '');
        const calls = native ? parts.filter(p => p.type === 'tool-call').map(p => ({ id: p.id, name: p.name, input: p.input })) : (m.tool_calls ?? []).map(p => ({ id: p.id, name: p.function.name, input: JSON.parse(p.function.arguments) }));
        return { role: m.role, text, calls };
      });
    }
    for (const text of ['[overflow-once] Read sample.txt', '[after-overflow-unique] Read sample.txt']) {
      const beforeSpans = all().length, beforeModels = models.length, beforeUsage = await nativeEvents();
      await prompt(overflow.id, text);
      const spans = all().slice(beforeSpans);
      const generations = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
      const requests = models.slice(beforeModels).filter(r => !r.rejected && !r.summary);
      assert.equal(generations.length, requests.length);
      for (let i = 0; i < generations.length; i++) {
        const input = JSON.parse(attrs(generations[i])['input.value']);
        assert.deepEqual(requestShape(input.messages, true), requestShape(requests[i].body.messages, false), `request ${i} for ${text}`);
        assert.ok(attrs(generations[i])['output.value'], 'tool-only generations have an output too');
      }
      assert.equal(attrs(spans.find(root))['fi.agent.outcome'], 'success');
      results[text.includes('after-overflow') ? 'afterOverflow' : 'overflowRecovery'] = { matchedRequests: requests.length, generations: generations.length,
        usage: await assertUsageDelta(spans, overflow.id, beforeUsage) };
    }

    // Reload while idle, then reuse the same conversation: no duplicated roots/exports.
    const reloadBefore = all().length;
    await client.location.reload();
    await prompt(session.id, '[after-reload] Explain the public sample');
    assert.equal(all().slice(reloadBefore).filter(root).length, 1);
    assert.equal(new Set(all().map(s => s.spanId)).size, all().length, 'no duplicate finalized span IDs');
    results.reload = true;

    await mkdir(`${project}/.opencode`, { recursive: true });
    await writeFile(`${project}/.opencode/futureagi.json`, JSON.stringify({ schemaVersion: 1, captureContent: false }), { mode: 0o600 });
    await client.location.reload();
    const privacyBefore = all().length;
    await prompt(session.id, '[private] Do not export this prompt body');
    const privateSpans = all().slice(privacyBefore);
    assert.ok(privateSpans.length >= 2);
    for (const span of privateSpans) {
      assert.equal(attrs(span)['input.value'], undefined);
      assert.equal(attrs(span)['output.value'], undefined);
    }
    results.projectPrivacy = true;

    const disabledBefore = all().length;
    const disabled = await run(process.execPath, ['/work/packages/traceai_agents/dist/cli.js', 'disable', 'opencode', '--json']);
    assert.equal(disabled.code, 0);
    await prompt(session.id, '[disabled] Explain the sample');
    await pause(1200);
    assert.equal(all().length, disabledBefore, 'running process respects persistent disable');
    results.disable = true;
    const uninstalled = await run(process.execPath, ['/work/packages/traceai_agents/dist/cli.js', 'uninstall', 'opencode', '--json']);
    assert.equal(uninstalled.code, 0);
    const hostConfig = JSON.parse(await readFile(`${env.XDG_CONFIG_HOME}/opencode/opencode.json`, 'utf8'));
    assert.ok(hostConfig.plugins.includes('/work/event-probe'));
    assert.ok(!hostConfig.plugins.some(p => typeof p === 'string' && p.startsWith('@traceai/opencode-plugin')));
    assert.equal(hostConfig.providers.mock.models.test.name, 'Test');
    results.ownedUninstall = true;
    await writeFile('/work/evidence/scenarios.json', JSON.stringify(results, null, 2));
  } finally {
    await writeFile('/work/evidence/scenarios.json', JSON.stringify(results, null, 2));
    await stopServer();
    await writeFile('/work/evidence/server.log', serverLog.replace(/server password .*/g, 'server password [REDACTED]'));
  }
}
