const { context, trace, SpanKind } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');
const { FITracer, setSession, setUser } = require('@traceai/fi-core');
(async () => {
  const tracer = new FITracer({ tracer: trace.getTracer('test-context') });
  const parent = tracer.startSpan('framework parent', { kind: SpanKind.INTERNAL, attributes: { 'gen_ai.span.kind': process.env.KIND || 'CHAIN' } });
  let active = trace.setSpan(context.active(), parent);
  active = setSession(active, { sessionId: 'session-test' });
  active = setUser(active, { userId: 'user-test' });
  if (process.env.SUPPRESS === 'true') active = suppressTracing(active);
  await context.with(active, async () => {
    const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'private' }] }) });
    await response.text();
  });
  parent.end();
  console.log(parent.spanContext().spanId);
})().catch(e => { console.error(e); process.exitCode = 1; });
