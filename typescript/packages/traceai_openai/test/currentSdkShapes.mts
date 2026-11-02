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

const predicted = {
  model: 'gpt-4.1', messages: [{ role: 'user', content: 'Rewrite this document' }],
  prediction: { type: 'content', content: 'SYNTHETIC-PREDICTED-PRIVATE' },
} satisfies OpenAI.Chat.Completions.ChatCompletionCreateParamsNonStreaming;
const predictedParts = {
  ...predicted, prediction: { type: 'content', content: [{ type: 'text', text: 'SYNTHETIC-PREDICTED-PRIVATE' }] },
} satisfies OpenAI.Chat.Completions.ChatCompletionCreateParamsNonStreaming;
const suffix = {
  model: 'gpt-3.5-turbo-instruct', prompt: 'hello', suffix: 'SYNTHETIC-SUFFIX-PRIVATE',
} satisfies OpenAI.Completions.CompletionCreateParamsNonStreaming;
const screenshot = {
  model: 'computer-use-preview', input: [{ type: 'computer_call_output', call_id: 'call_synthetic',
    output: { type: 'computer_screenshot', image_url: 'https://example.invalid/SYNTHETIC-SCREENSHOT-PRIVATE' },
  }],
} satisfies OpenAI.Responses.ResponseCreateParamsNonStreaming;
const screenshotFile = {
  ...screenshot, input: [{ type: 'computer_call_output', call_id: 'call_synthetic',
    output: { type: 'computer_screenshot', file_id: 'file-SYNTHETIC-SCREENSHOT-PRIVATE' },
  }],
} satisfies OpenAI.Responses.ResponseCreateParamsNonStreaming;
void [predicted, predictedParts, suffix, screenshot, screenshotFile];

const discoveredMcp = {
  model: 'synthetic',
  input: [{ type: 'tool_search_output', call_id: 'call-search', execution: 'client', tools: [{
    type: 'mcp', server_label: 'synthetic', server_url: 'https://synthetic:private@example.invalid/mcp?api_key=private',
    authorization: 'synthetic', headers: { 'X-Api-Key': 'synthetic' },
  }] }],
} satisfies OpenAI.Responses.ResponseCreateParamsNonStreaming;
const responseCustomCall = {
  type: 'custom_tool_call', id: 'ctc-synthetic', call_id: 'call-synthetic',
  name: 'code_exec', input: 'print(1)', status: 'completed',
} satisfies OpenAI.Responses.ResponseCustomToolCallItem;
const responseCustomHistory = {
  model: 'synthetic', input: [responseCustomCall,
    { type: 'custom_tool_call_output', call_id: responseCustomCall.call_id, output: '1' }],
} satisfies OpenAI.Responses.ResponseCreateParamsNonStreaming;
void [discoveredMcp, responseCustomCall, client.responses.create(responseCustomHistory)];
