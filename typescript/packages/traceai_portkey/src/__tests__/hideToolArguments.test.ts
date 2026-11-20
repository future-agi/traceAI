/**
 * Tool parameters / arguments under hideInputs / hideOutputs, through the real PortkeyInstrumentation
 * on a stubbed Portkey resource (no network). Portkey writes tool data only inside serialized JSON
 * attributes (input.value, raw.input, llm.input_messages, llm.output_messages, raw.output), all via
 * FITracer; it never writes tool.parameters / tool_call.function.arguments style keys.
 */
import { PortkeyInstrumentation, _resetPatchedStateForTesting } from '../instrumentation';
import { InMemorySpanExporter, SimpleSpanProcessor, ReadableSpan } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';

const IN_ARGS = 'SYNTH-INPUT-TOOL-ARGS-7731';
const IN_SCHEMA = 'SYNTH-TOOL-PARAM-SCHEMA-4410';
const IN_DESC = 'SYNTH-TOOL-DESCRIPTION-9902';
const OUT_ARGS = 'SYNTH-OUTPUT-TOOL-ARGS-5518';

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let inst: PortkeyInstrumentation;
afterEach(() => { inst?.disable(); _resetPatchedStateForTesting(); });
afterAll(async () => { await provider.shutdown(); });
const dump = (s: ReadableSpan) => JSON.stringify({ a: s.attributes, e: s.events, s: s.status });

async function run(traceConfig: object) {
  exporter.reset();
  inst = new PortkeyInstrumentation({ traceConfig } as never);
  inst.setTracerProvider(provider);
  const chat = async (_b?: unknown) => ({
    id: 'c1', model: 'm',
    choices: [{ finish_reason: 'tool_calls', message: { role: 'assistant', content: null,
      tool_calls: [{ id: 'call_1', type: 'function', function: { name: 'lookup', arguments: JSON.stringify({ q: OUT_ARGS }) } }] } }],
  });
  const responses = async (_b?: unknown) => ({
    id: 'r1', status: 'completed', model: 'm',
    output: [{ type: 'function_call', call_id: 'call_1', name: 'lookup', arguments: JSON.stringify({ q: OUT_ARGS }) }],
  });
  const client = { chat: { completions: { create: chat } }, responses: { create: responses } };
  inst.manuallyInstrument(client);
  const tool = { type: 'function', function: { name: 'lookup', description: IN_DESC,
    parameters: { type: 'object', properties: { q: { type: 'string', description: IN_SCHEMA } } } } };
  await client.chat.completions.create({ model: 'm', tools: [tool], messages: [
    { role: 'user', content: 'hi' },
    { role: 'assistant', content: null, tool_calls: [{ id: 'call_0', type: 'function', function: { name: 'lookup', arguments: JSON.stringify({ q: IN_ARGS }) } }] },
    { role: 'tool', tool_call_id: 'call_0', content: 'ok' },
  ] });
  await client.responses.create({ model: 'm', tools: [{ type: 'function', name: 'lookup', description: IN_DESC,
    parameters: { type: 'object', properties: { q: { type: 'string', description: IN_SCHEMA } } } }],
    input: [{ type: 'function_call', call_id: 'call_0', name: 'lookup', arguments: JSON.stringify({ q: IN_ARGS }) }] });
  await new Promise((r) => setTimeout(r, 20));
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(2);
  return spans;
}

it('control: unhidden export contains the synthetic secrets', async () => {
  const all = (await run({})).map(dump).join('\n');
  for (const s of [IN_ARGS, IN_SCHEMA, OUT_ARGS]) expect(all).toContain(s);
});

it('hideInputs: no input-side tool argument, schema or description is exported', async () => {
  for (const span of await run({ hideInputs: true })) {
    for (const s of [IN_ARGS, IN_SCHEMA, IN_DESC]) expect(dump(span)).not.toContain(s);
  }
});

it('hideOutputs: no output-side tool call arguments are exported', async () => {
  for (const span of await run({ hideOutputs: true })) expect(dump(span)).not.toContain(OUT_ARGS);
});
