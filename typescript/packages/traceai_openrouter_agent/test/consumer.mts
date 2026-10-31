import { OpenRouter } from '@openrouter/sdk';
import { callModel } from '@openrouter/agent';
import { buildTracerProvider, createTraceHooks, createTraceSession } from '@traceai/openrouter-agent';

const provider = buildTracerProvider({ spanProcessors: [] });
const client = new OpenRouter({ apiKey: 'synthetic-not-a-key' });
const telemetry = createTraceSession(provider, { captureContent: false });
const result = callModel(client, { model: 'synthetic/model', input: 'hello', hooks: telemetry.hooks });
const text: Promise<string> = telemetry.getText(result);
const chunks: AsyncGenerator<string> = telemetry.getTextStream(result);
const legacy = callModel(client, { model: 'synthetic/model', input: 'hello', hooks: createTraceHooks(provider) });
void text; void chunks; void legacy;
