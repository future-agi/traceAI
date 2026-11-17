// An application with no OpenTelemetry of its own, so no context manager, with the agent's
// incoming HTTP instrumentation on. A request handler starts one AI span the way an
// integration the agent does not scope starts its model span (Groq, BeeAI or the AI SDK 7
// telemetry): with an agent-bound tracer, outside any patched SDK call. After that call has
// returned, while its span still records, the handler sends a plain fetch to the chat route.
// The plain fetch has no AI integration of its own, so its wire span is its only record.
// MODE=active-span-left-open starts the span with fi-core's startActiveSpan instead, whose
// callback returns before the span ends.
const { createServer } = require('node:http');
const { dirname, join } = require('node:path');
const { FITracer } = require('@traceai/fi-core');
const auto = require('@traceai/auto');
// The agent's own build of the module that binds its tracers, the same instance the
// --import entry loaded. Calling init() again changes nothing and returns the agent's provider.
const { contextBoundProvider } = require(join(dirname(require.resolve('@traceai/auto')), 'agent-context.js'));
const provider = auto.init();
// The tracer an integration gets from the agent: an FI tracer over an agent-bound tracer.
const integration = new FITracer({ tracer: FITracer.autoProvider(contextBoundProvider(provider)).getTracer('unscoped-integration') });
const mode = process.env.MODE || 'unscoped';
(async () => {
  const server = createServer(async (req, res) => {
    let span;
    if (mode === 'active-span-left-open') {
      integration.startActiveSpan('span left open', { attributes: { 'gen_ai.span.kind': 'LLM', 'input.value': 'a call whose span outlives its callback' } }, opened => { span = opened; });
    } else {
      span = integration.startSpan('unscoped model call', { attributes: { 'gen_ai.span.kind': 'LLM', 'input.value': 'a question sent through an unscoped integration' } });
    }
    // The AI call has returned and its span still records.
    const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'a plain fetch after the AI call' }] }) });
    const answer = await response.json();
    span.end();
    res.end(answer.choices[0].message.content);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/handler`);
  const answer = await response.text();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  console.log(JSON.stringify({ mode, answer }));
})().catch(e => { console.error(e); process.exitCode = 1; });
