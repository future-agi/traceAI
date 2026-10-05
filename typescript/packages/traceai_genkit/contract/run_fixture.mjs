/**
 * Contract fixture for the shared Python harness (python/tests/harness).
 *
 * Runs real genkit 1.42.0 flows with Genkit's own `mockModel` (genkit/testing)
 * and a tool, with the BUILT @traceai/genkit package (imported by package name,
 * so the package.json "exports" map is exercised) and the real @traceai/fi-core
 * OTLP/HTTP exporter. No model vendor is called: the model is a local mock.
 *
 * Env:
 *   FI_BASE_URL, FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME   read by fi-core register()
 *   GENKIT_TELEMETRY_SERVER   optional; Genkit's own dev-UI trace exporter target
 *   JOURNEY                   tool | stream | error | schema | agent | sigterm | devserver
 *   CAPTURE_CONTENT=1         captureContent: true
 *   SESSION_ID                wrap the flow call in fi-core setSession()
 *   FI_GLOBAL_PROVIDER=1      misconfiguration control: register() as the global provider
 *   SIGNAL_MODE               sigterm journey: helper (flushOnSignals, default) | plain (app listener only)
 *
 * Prints one JSON line on stdout: {"journey", "result"|"error", "traceIds", ...}.
 */
import { context } from '@opentelemetry/api';
import { register, ProjectType, setSession } from '@traceai/fi-core';
import { FIGenkitSpanProcessor, flushOnSignals } from '@traceai/genkit';
import { genkit, z } from 'genkit';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { mockModel } from 'genkit/testing';

export const PROMPT_MARKER = 'SECRET_PROMPT_MARKER';
export const OUTPUT_MARKER = 'SECRET_OUTPUT_MARKER';
export const TOOL_OUTPUT_MARKER = 'SECRET_TOOL_OUTPUT_MARKER';
export const CHUNK_MARKER = 'SECRET_CHUNK_MARKER';

const journey = process.env.JOURNEY || 'tool';

// 1. Future AGI provider. setGlobalTracerProvider:false so Genkit's NodeSDK owns the global provider.
const tracerProvider = register({
  projectType: ProjectType.OBSERVE,
  setGlobalTracerProvider: process.env.FI_GLOBAL_PROVIDER === '1',
});

// 2. Genkit telemetry with our processor appended after Genkit's own telemetry-server processor.
const processor = new FIGenkitSpanProcessor({
  tracerProvider,
  captureContent: process.env.CAPTURE_CONTENT === '1',
});
await enableTelemetry({ spanProcessors: [processor] });

const ai = genkit({});

// Model call 1 asks for the tool; model call 2 answers. Usage per call: 11/3/14 and 21/4/25.
const toolModel = mockModel(ai, {
  name: 'contract/tool-model',
  respond: (request) => {
    const last = request.messages.at(-1);
    if (last?.content?.some((part) => part.toolResponse)) {
      return { text: `final ${OUTPUT_MARKER}`, usage: { inputTokens: 21, outputTokens: 4, totalTokens: 25 } };
    }
    return {
      toolRequests: [{ name: 'lookup', input: { id: 7 } }],
      usage: { inputTokens: 11, outputTokens: 3, totalTokens: 14 },
    };
  },
});

// One streamed call: three chunks, usage 9/6/15.
const streamModel = mockModel(ai, {
  name: 'contract/stream-model',
  respond: (_request, { sendChunk }) => {
    sendChunk('one ');
    sendChunk('two ');
    sendChunk(CHUNK_MARKER);
    return { text: `one two ${CHUNK_MARKER}`, usage: { inputTokens: 9, outputTokens: 6, totalTokens: 15 } };
  },
});

const lookup = ai.defineTool(
  { name: 'lookup', description: 'look up a record', inputSchema: z.object({ id: z.number() }), outputSchema: z.string() },
  async ({ id }) => `${TOOL_OUTPUT_MARKER}-${id}`
);
const brokenLookup = ai.defineTool(
  { name: 'brokenLookup', description: 'always fails', inputSchema: z.object({ id: z.number() }), outputSchema: z.string() },
  async () => {
    throw new Error('lookup exploded');
  }
);
const brokenModel = mockModel(ai, {
  name: 'contract/broken-model',
  respond: () => ({ toolRequests: [{ name: 'brokenLookup', input: { id: 1 } }], usage: { inputTokens: 5, outputTokens: 1, totalTokens: 6 } }),
});

const qaFlow = ai.defineFlow({ name: 'qaFlow', inputSchema: z.string(), outputSchema: z.string() }, async (question) => {
  const prepared = await ai.run('prepare', async () => question.trim());
  const response = await ai.generate({ model: toolModel, prompt: prepared, tools: [lookup] });
  return response.text;
});

const streamFlow = ai.defineFlow(
  { name: 'streamFlow', inputSchema: z.string(), outputSchema: z.string(), streamSchema: z.string() },
  async (question, { sendChunk }) => {
    const { stream, response } = ai.generateStream({ model: streamModel, prompt: question });
    for await (const chunk of stream) sendChunk(chunk.text);
    return (await response).text;
  }
);

const failingFlow = ai.defineFlow({ name: 'failingFlow', inputSchema: z.string(), outputSchema: z.string() }, async (question) => {
  const response = await ai.generate({ model: brokenModel, prompt: question, tools: [brokenLookup] });
  return response.text;
});

// Structured output the schema rejects: `answer` must be a string. Genkit's
// ValidationError (@genkit-ai/core src/schema.ts:78) embeds the model output
// after "Provided data:" and instrumentation.ts:156-162 writes that message to
// the generate and flow spans' status and exception events.
export const STRUCTURED_OUTPUT_TEXT = JSON.stringify({ answer: 42, note: 'SECRET_STRUCTURED_OUTPUT_MARKER' });
const structuredModel = mockModel(ai, {
  name: 'contract/structured-model',
  respond: () => ({ text: STRUCTURED_OUTPUT_TEXT, usage: { inputTokens: 7, outputTokens: 5, totalTokens: 12 } }),
});
const structuredFlow = ai.defineFlow({ name: 'structuredFlow', inputSchema: z.string() }, async (question) => {
  const response = await ai.generate({ model: structuredModel, prompt: question, output: { schema: z.object({ answer: z.string() }) } });
  return response.output;
});

function withSession(fn) {
  const sessionId = process.env.SESSION_ID;
  if (!sessionId) return fn();
  return context.with(setSession(context.active(), { sessionId }), fn);
}

function print(payload) {
  process.stdout.write(JSON.stringify({ journey, ...payload }) + '\n');
}

async function flushAndExit(payload) {
  // The documented shutdown path: Genkit's flushTracing() calls forceFlush() on
  // every processor in TelemetryConfig.spanProcessors, ours included.
  let flushError = null;
  try {
    await flushTracing();
  } catch (error) {
    flushError = String(error);
  }
  print({ ...payload, flushError });
  process.exit(0);
}

const prompt = `  ${PROMPT_MARKER}  `;

if (journey === 'tool') {
  const started = Date.now();
  const result = await withSession(() => qaFlow(prompt));
  await flushAndExit({ result, flowMillis: Date.now() - started });
} else if (journey === 'stream') {
  const chunks = [];
  const streamed = withSession(() => streamFlow.stream(prompt));
  for await (const chunk of streamed.stream) chunks.push(chunk);
  const result = await streamed.output;
  await flushAndExit({ result, chunks });
} else if (journey === 'error') {
  try {
    await failingFlow(prompt);
    await flushAndExit({ error: null });
  } catch (error) {
    await flushAndExit({ error: String(error && error.message) });
  }
} else if (journey === 'schema') {
  try {
    const result = await structuredFlow(prompt);
    await flushAndExit({ result, error: null });
  } catch (error) {
    await flushAndExit({ error: String(error && error.message), modelOutput: STRUCTURED_OUTPUT_TEXT });
  }
} else if (journey === 'agent') {
  // Beta agents (genkit/beta defineAgent): Genkit tags the agent span with
  // genkit:metadata:agent:sessionId (@genkit-ai/ai src/agent.ts:1061-1063).
  const { genkit: genkitBeta } = await import('genkit/beta');
  const beta = genkitBeta({});
  const agentModel = mockModel(beta, {
    name: 'contract/agent-model',
    respond: () => ({ text: `agent ${OUTPUT_MARKER}`, usage: { inputTokens: 3, outputTokens: 2, totalTokens: 5 } }),
  });
  const agent = beta.defineAgent({ name: 'supportAgent', model: agentModel, system: 'Answer briefly.' });
  const chat = agent.chat();
  const response = await chat.send(PROMPT_MARKER);
  await flushAndExit({ sessionId: response.sessionId, result: response.text });
} else if (journey === 'sigterm') {
  // AC-06: no explicit flush after the flow; the process is signalled while it is still serving.
  // genkit/src/genkit.ts:786-793 registers a SIGTERM listener at import that calls process.exit(0)
  // after stopping reflection servers; it does not flush.
  setInterval(() => undefined, 1000); // a long-running server keeps the event loop alive
  if ((process.env.SIGNAL_MODE || 'helper') === 'helper') {
    // Documented path: flush first, then let Genkit's own listeners run (and exit).
    flushOnSignals(async () => {
      await flushTracing();
      print({ result: globalThis.__result, signal: 'SIGTERM', flushed: true });
    });
  } else {
    // Control: an app listener that awaits flushTracing() races Genkit's exit listener.
    process.on('SIGTERM', async () => {
      await flushTracing();
      print({ result: globalThis.__result, signal: 'SIGTERM', flushed: true });
      process.exit(0);
    });
  }
  globalThis.__result = await qaFlow(prompt);
  process.kill(process.pid, 'SIGTERM');
} else if (journey === 'devserver') {
  // AC-01: GENKIT_ENV=dev starts Genkit's reflection server (what the Dev UI
  // drives). The harness calls /api/runAction, then sends SIGTERM.
  flushOnSignals(async () => {
    await flushTracing();
    print({ signal: 'SIGTERM', flushed: true });
  });
  print({ ready: true, pid: process.pid });
  setInterval(() => undefined, 1000);
} else {
  throw new Error(`unknown JOURNEY ${journey}`);
}
