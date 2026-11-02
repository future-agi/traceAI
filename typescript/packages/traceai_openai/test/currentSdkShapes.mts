import OpenAI from 'openai';
// Compile-only witnesses against the installed released SDK. No vendor calls.
declare const client: OpenAI;
const storedPrompt: OpenAI.Responses.ResponseCreateParamsNonStreaming = {
  model: 'synthetic',
  prompt: {
    id: 'pmpt_synthetic',
    variables: {
      text: 'synthetic',
      image: { type: 'input_image', image_url: 'https://example.invalid/synthetic', detail: 'auto' },
    },
  },
};
const mcp: OpenAI.Responses.ResponseCreateParamsNonStreaming = {
  model: 'synthetic', input: 'synthetic',
  tools: [{
    type: 'mcp', server_label: 'synthetic', server_url: 'https://example.invalid/mcp',
    authorization: 'synthetic', headers: { Authorization: 'Bearer synthetic' },
  }],
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
const customDelta: OpenAI.Chat.Completions.ChatCompletionChunk = {
  id: 'chat-synthetic', created: 1, object: 'chat.completion.chunk', model: 'synthetic',
  choices: [{ index: 0, delta: {
    tool_calls: [{ index: 0, id: 'call-synthetic', type: 'custom', custom: { name: 'code_exec', input: 'print(' } }],
  }, finish_reason: null }],
};
const parallelDelta: OpenAI.Chat.Completions.ChatCompletionChunk = {
  id: 'chat-synthetic', created: 1, object: 'chat.completion.chunk', model: 'synthetic',
  choices: [{ index: 0, delta: {
    tool_calls: [{ index: 1, id: 'call-b', type: 'function', function: { name: 'second', arguments: '{"b":' } }],
  }, finish_reason: null }],
};
void [client.responses.create(storedPrompt), client.responses.create(mcp),
  client.chat.completions.create(request), client.completions.create(stream), customDelta, parallelDelta];
