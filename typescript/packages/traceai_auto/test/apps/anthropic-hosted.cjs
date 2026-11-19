// Zero-code Anthropic on the cloud-hosted clients. The application directory holds only the packages the
// scenario installs; no instrumentation package is imported or constructed by the application.
const assert = require('node:assert/strict');
const auto = require('@traceai/auto');
const spans = [];
const exporter = { export(batch, done) { spans.push(...batch); done({ code: 0 }); }, async shutdown() {} };
const message = { id: 'msg_1', type: 'message', role: 'assistant', model: 'm', content: [{ type: 'text', text: 'hello' }], stop_reason: 'end_turn', stop_sequence: null, usage: { input_tokens: 2, output_tokens: 1 } };
const fetch = async () => new Response(JSON.stringify(message), { status: 200, headers: { 'content-type': 'application/json' } });
(async () => {
  try {
    auto.init({ exporter, configFile: '/does-not-exist/traceai-fi-config', instrumentations: ['anthropic'] });
    const before = auto.report().integrations.find(s => s.name === 'anthropic').status;
    if (process.env.APP_BASE_FIRST === '1') require('@anthropic-ai/sdk');
    const { AnthropicBedrock } = require('@anthropic-ai/bedrock-sdk');
    const client = new AnthropicBedrock({ awsRegion: 'us-east-1', awsAccessKey: 'a', awsSecretKey: 'b', maxRetries: 0, fetch });
    await client.messages.create({ model: 'anthropic.claude-3', max_tokens: 4, messages: [{ role: 'user', content: 'hi' }] });
    await auto.forceFlush();
    const status = auto.report().integrations.find(s => s.name === 'anthropic');
    const span = spans.find(s => s.attributes['gen_ai.provider.name'] === 'aws.bedrock');
    console.log(JSON.stringify({ before, status: status.status, reason: status.reason, provider: span && span.attributes['gen_ai.provider.name'] }));
  } finally { await auto.shutdown(); }
})();
