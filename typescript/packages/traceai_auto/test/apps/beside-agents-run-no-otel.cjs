// An application with no OpenTelemetry of its own, so no context manager. In the same tick it
// starts one OpenAI Agents SDK run, whose model sends one chat request, and one plain fetch to
// the same chat route. The plain fetch has no AI integration of its own, so only wire capture
// can record it. It loads traceAI only when TRACED is set, after both requests, to print the
// integration's status.
const agents = require('@openai/agents');
function plainFetch() {
  return globalThis.fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'a plain fetch beside the agent run' }] }) })
    .then(response => response.json());
}
(async () => {
  // The SDK's Chat Completions model, sent to the mock provider.
  const modelProvider = new agents.OpenAIProvider({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', useResponses: false });
  const runner = new agents.Runner({ modelProvider, workflowName: 'beside agent run' });
  const agent = new agents.Agent({ name: 'Assistant', instructions: 'Answer the user.', model: 'gpt-fixture' });
  // The agent run starts first and the plain fetch second, in the same tick.
  const [result, plain] = await Promise.all([runner.run(agent, 'a question sent through the agent run'), plainFetch()]);
  const status = process.env.TRACED ? require('@traceai/auto').report().integrations.find(integration => integration.name === 'openai-agents')?.status ?? null : null;
  console.log(JSON.stringify({ finalOutput: result.finalOutput, plain: plain.choices[0].message.content, status }));
})().catch(error => { console.error(error); process.exitCode = 1; });
