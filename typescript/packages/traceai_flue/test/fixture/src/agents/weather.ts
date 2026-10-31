'use agent';
import { setProvider, useModel } from '@flue/runtime';
import { createProvider } from '@earendil-works/pi-ai';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';
import '../telemetry.ts';

setProvider(createProvider({
  id: 'synthetic',
  auth: { apiKey: { name: 'synthetic', resolve: async () => ({ auth: { apiKey: 'not-a-real-key' } }) } },
  models: [{
    id: 'local-model', name: 'local-model', api: 'openai-completions', provider: 'synthetic',
    baseUrl: process.env.MODEL_BASE_URL, reasoning: false, input: ['text'],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 4096, maxTokens: 256,
  }],
  api: openAICompletionsApi(),
}));
export function Weather() {
  useModel('synthetic/local-model');
  return 'Answer the weather question briefly.';
}
