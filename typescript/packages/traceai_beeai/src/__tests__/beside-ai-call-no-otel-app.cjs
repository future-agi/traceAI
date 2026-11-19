// Copied into a temporary application with the workspace's BeeAI framework and no
// OpenTelemetry of its own: no context manager, tracer provider or propagator, and no manual
// instrumentors. It makes one chat model call, whose model sends one request to the provider,
// and one plain fetch to the same chat route while that call is open. The plain fetch has no
// AI integration of its own, so only wire capture can record it.
const assert = require("node:assert/strict");
const { ChatModel, ChatModelOutput } = require("beeai-framework/backend/chat");
const { AssistantMessage, UserMessage } = require("beeai-framework/backend/message");
const { Emitter } = require("beeai-framework/emitter/emitter");
const { auto, contextManager } = globalThis.__beeaiNoOtelTest;
const [mode] = process.argv.slice(2);
const traced = mode === "traced";
const { SDK_PROMPT, PLAIN_PROMPT } = process.env;
assert(SDK_PROMPT && PLAIN_PROMPT);

const post = content => fetch(process.env.MOCK_URL + "/v1/chat/completions", {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ model: "fixture", messages: [{ role: "user", content }] }),
}).then(response => { assert.equal(response.status, 200); return response.json(); });
// The application's own request, with no SDK. Its answer is read to the end.
const plainFetch = () => post(PLAIN_PROMPT);

let providerCalls = 0, modelText;
// A BeeAI chat model whose transport is one request to the provider's chat route, as a
// provider adapter would send it.
class LoopbackChatModel extends ChatModel {
  emitter = Emitter.root.child({ namespace: ["backend", "loopback", "chat"], creator: this });
  modelId = "fixture";
  providerId = "loopback";
  async _create() {
    providerCalls++;
    const completion = await post(SDK_PROMPT);
    modelText = completion.choices[0].message.content;
    return new ChatModelOutput([new AssistantMessage(modelText)], { promptTokens: 4, completionTokens: 2, totalTokens: 6 });
  }
  async *_createStream(input, run) { yield await this._create(input, run); }
}

(async () => {
  const model = new LoopbackChatModel();
  // The model call starts first and the plain fetch second, in the same tick.
  const [output, plain] = await Promise.all([model.create({ messages: [new UserMessage(SDK_PROMPT)] }), plainFetch()]);
  assert(output instanceof ChatModelOutput);
  assert.equal(providerCalls, 1);
  const report = traced ? auto.report() : undefined;
  if (traced) await auto.forceFlush();
  console.log(JSON.stringify({
    application: { text: modelText, plain: plain.choices[0].message.content, providerCalls },
    contextManager: contextManager(),
    active: report?.active ?? false,
    beeai: report?.integrations.find(item => item.name === "beeai")?.status ?? null,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (traced) await auto.shutdown();
});
