/**
 * Span inventory for the pinned genkit version (TH-8240, architecture section 1).
 *
 * Runs one flow with Genkit's own `mockModel` (genkit/testing) and one tool,
 * once with generate() and once with streamFlow, and records every span Genkit
 * hands to a span processor passed through `enableTelemetry({ spanProcessors })`.
 * No network: GENKIT_TELEMETRY_SERVER is left unset, so Genkit's own
 * TraceServerExporter skips the POST, and nothing is exported anywhere else.
 *
 * Usage: node contract/inventory.mjs [--values]
 * Prints JSON: one entry per span with name, genkit:type, subtype, and the
 * attribute keys with their JS types. `--values` also prints values (the dump
 * then contains the fixture prompt and model output).
 */
import { genkit, z } from 'genkit';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { mockModel } from 'genkit/testing';

const showValues = process.argv.includes('--values');
const spans = [];
const recorder = {
  onStart() {},
  onEnd(span) {
    spans.push(span);
  },
  forceFlush: async () => {},
  shutdown: async () => {},
};

await enableTelemetry({ spanProcessors: [recorder] });

const ai = genkit({});
const model = mockModel(ai, {
  name: 'inventory/mock',
  respond: (req, { sendChunk }) => {
    const last = req.messages.at(-1);
    if (last?.content?.some((p) => p.toolResponse)) {
      sendChunk('final ');
      return { text: 'final answer', usage: { inputTokens: 21, outputTokens: 4, totalTokens: 25 } };
    }
    return {
      toolRequests: [{ name: 'lookup', input: { id: 7 } }],
      usage: { inputTokens: 11, outputTokens: 3, totalTokens: 14 },
    };
  },
});
const lookup = ai.defineTool(
  { name: 'lookup', description: 'look up a record', inputSchema: z.object({ id: z.number() }), outputSchema: z.string() },
  async ({ id }) => `record-${id}`
);
const flow = ai.defineFlow({ name: 'inventoryFlow', inputSchema: z.string(), outputSchema: z.string() }, async (q, { sendChunk }) => {
  const step = await ai.run('prepare', async () => q.trim());
  const { stream, response } = ai.generateStream({ model, prompt: step, tools: [lookup] });
  for await (const chunk of stream) sendChunk(chunk.text);
  return (await response).text;
});

await flow('  hello  ');
const streamed = flow.stream('  streamed  ');
for await (const _ of streamed.stream) {
  // drain
}
await streamed.output;
await flushTracing();

const out = spans.map((s) => {
  const attrs = s.attributes;
  const entry = {
    name: s.name,
    'genkit:type': attrs['genkit:type'],
    'genkit:metadata:subtype': attrs['genkit:metadata:subtype'],
    parentSpanId: s.parentSpanId ?? s.parentSpanContext?.spanId ?? null,
    spanId: s.spanContext().spanId,
    status: s.status,
    events: s.events.map((e) => e.name),
    keys: Object.fromEntries(Object.entries(attrs).map(([k, v]) => [k, typeof v])),
    readableSpanShape: {
      hasParentSpanId: 'parentSpanId' in s,
      hasParentSpanContext: 'parentSpanContext' in s,
      hasInstrumentationLibrary: 'instrumentationLibrary' in s,
      hasInstrumentationScope: 'instrumentationScope' in s,
      scope: (s.instrumentationScope ?? s.instrumentationLibrary)?.name,
    },
  };
  if (showValues) entry.values = attrs;
  return entry;
});
process.stdout.write(JSON.stringify(out, null, 2) + '\n');
process.exit(0);
