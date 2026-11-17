// No application OpenTelemetry setup. The agent's own context helpers set session,
// user and metadata for AI spans, or suppress them.
const { context, ROOT_CONTEXT } = require('@opentelemetry/api');
const { setTags } = require('@traceai/fi-core');
const auto = require('@traceai/auto');
async function ai() {
  const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] }) });
  await response.text();
}
(async () => {
  if (process.env.SUPPRESS === 'true') await auto.withTracingSuppressed(ai);
  else {
    await auto.withSession('session-test', () => auto.withUser('user-test', () => auto.withMetadata({ tenant: 'tenant-test' }, ai)));
    // withContext runs the call with exactly the context it is given.
    await auto.withContext(setTags(ROOT_CONTEXT, ['tag-test']), ai);
  }
  console.log(JSON.stringify({ context: context._getContextManager().constructor.name }));
})().catch(error => { console.error(error); process.exitCode = 1; });
