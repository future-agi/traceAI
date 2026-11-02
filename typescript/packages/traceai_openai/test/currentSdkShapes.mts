import OpenAI from 'openai';
// Compile-only witnesses against the installed released SDK. No vendor calls.
declare const client: OpenAI;
const storedPrompt: OpenAI.Responses.ResponseCreateParamsNonStreaming = {
  model: 'synthetic', prompt: { id: 'pmpt_synthetic' },
};
const custom: OpenAI.Chat.Completions.ChatCompletionMessageCustomToolCall = {
  id: 'call-synthetic', type: 'custom', custom: { name: 'code_exec', input: 'print(1)' },
};
const request: OpenAI.Chat.Completions.ChatCompletionCreateParamsNonStreaming = {
  model: 'synthetic',
  messages: [
    { role: 'assistant', content: null, tool_calls: [custom] },
    { role: 'tool', tool_call_id: custom.id, content: '1' },
  ],
  tools: [{ type: 'custom', custom: { name: 'code_exec' } }],
};
const stream: OpenAI.Completions.CompletionCreateParamsStreaming = {
  model: 'synthetic', prompt: 'synthetic', stream: true, stream_options: { include_usage: true },
};
void [client.responses.create(storedPrompt), client.chat.completions.create(request), client.completions.create(stream)];
