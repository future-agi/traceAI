// Zero-code Bedrock Agents: the application only requires the agent-runtime SDK after the
// agent started; no instrumentation package is imported or constructed by the application.
const assert = require('node:assert/strict');
const auto = require('@traceai/auto');
const spans = [];
const exporter = { export(batch, done) { spans.push(...batch); done({ code: 0 }); }, async shutdown() {} };
(async () => {
  try {
    auto.init({ exporter, configFile: '/does-not-exist/traceai-fi-config', instrumentations: ['bedrock'] });
    const { BedrockAgentRuntimeClient, InvokeAgentCommand } = require('@aws-sdk/client-bedrock-agent-runtime');
    const bytes = text => new TextEncoder().encode(text);
    async function* completion() {
      yield { trace: { agentId: 'AGENT1', agentAliasId: 'ALIAS1', sessionId: 's-1', trace: { orchestrationTrace: { modelInvocationInput: { traceId: 't-1', foundationModel: 'anthropic.claude-3-haiku', text: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }) } } } } };
      yield { trace: { agentId: 'AGENT1', agentAliasId: 'ALIAS1', sessionId: 's-1', trace: { orchestrationTrace: { modelInvocationOutput: { traceId: 't-1', parsedResponse: { text: 'hello' }, metadata: { usage: { inputTokens: 3, outputTokens: 2 } } } } } } };
      yield { chunk: { bytes: bytes('hello ') } };
      yield { chunk: { bytes: bytes('world') } };
    }
    const client = new BedrockAgentRuntimeClient({ region: 'us-east-1', credentials: { accessKeyId: 'a', secretAccessKey: 'b' } });
    client.middlewareStack.add(() => async () => ({ output: { completion: completion(), sessionId: 's-1' }, response: {} }), { step: 'initialize', name: 'recorded', priority: 'low' });
    const response = await client.send(new InvokeAgentCommand({ agentId: 'AGENT1', agentAliasId: 'ALIAS1', sessionId: 's-1', inputText: 'hi', enableTrace: true }));
    let text = '';
    for await (const event of response.completion) if (event.chunk) text += new TextDecoder().decode(event.chunk.bytes);
    assert.equal(text, 'hello world');
    await auto.forceFlush();
    const status = auto.report().integrations.find(s => s.name === 'bedrock');
    assert.equal(status.status, 'instrumented', JSON.stringify(status));
    const agent = spans.find(s => s.name === 'invoke_agent AGENT1/ALIAS1');
    assert(agent, JSON.stringify(spans.map(s => s.name)));
    const llm = spans.find(s => (s.attributes['fi.span.kind'] ?? s.attributes['gen_ai.span.kind']) === 'LLM' && s.name !== agent.name);
    assert(llm, JSON.stringify(spans.map(s => [s.name, s.attributes])));
    console.log(JSON.stringify({ status: status.status, agentKind: agent.attributes['fi.span.kind'] ?? agent.attributes['gen_ai.span.kind'], output: agent.attributes['output.value'], childOfAgent: (llm.parentSpanContext ?? { spanId: llm.parentSpanId }).spanId === agent.spanContext().spanId, tokens: llm.attributes['gen_ai.usage.input_tokens'] }));
  } finally { await auto.shutdown(); }
})();
