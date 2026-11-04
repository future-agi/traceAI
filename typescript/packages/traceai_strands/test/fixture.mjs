import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
export const require = createRequire(process.env.STRANDS_TEST_ROOT ? `${process.env.STRANDS_TEST_ROOT}/package.json` : import.meta.url);
let entry;
try { entry = require.resolve('@strands-agents/sdk'); } catch (error) {
  // SDK 0.1.0 has only an import condition, so the CJS resolver cannot find it.
  if (error.code !== 'ERR_PACKAGE_PATH_NOT_EXPORTED' || !process.env.STRANDS_TEST_ROOT) throw error;
  const metadata = require(`${process.env.STRANDS_TEST_ROOT}/node_modules/@strands-agents/sdk/package.json`);
  entry = `${process.env.STRANDS_TEST_ROOT}/node_modules/@strands-agents/sdk/${metadata.exports['.'].import}`;
}
export const sdk = await import(pathToFileURL(entry));
const { Agent, FunctionTool } = sdk;
// 0.1.0 did not re-export its Model base class; use the actual bundled base.
const Model = sdk.Model ?? Object.getPrototypeOf(sdk.BedrockModel);
export const SECRET = 'synthetic-sensitive-content';
export class FixtureModel extends Model {
  constructor(mode = 'text') { super(); this.mode = mode; this.calls = 0; }
  getConfig() { return { modelId: 'fixture-model' }; }
  updateConfig() {}
  async *stream(messages, options) {
    this.calls++;
    await new Promise((resolve) => setImmediate(resolve));
    if (this.mode === 'error') throw new Error(SECRET);
    yield { type: 'modelMessageStartEvent', role: 'assistant' };
    const useTool = this.mode === 'tool' && this.calls === 1;
    yield { type: 'modelContentBlockStartEvent', ...(useTool ? {
      start: { type: 'toolUseStart', name: 'echo', toolUseId: 'call-1' },
    } : {}) };
    yield { type: 'modelContentBlockDeltaEvent', delta: useTool
      ? { type: 'toolUseInputDelta', input: JSON.stringify({ text: SECRET }) }
      : { type: 'textDelta', text: this.mode === 'swarm'
        ? JSON.stringify({ message: SECRET }) : SECRET } };
    await new Promise((resolve) => setImmediate(resolve));
    if (options?.cancelSignal?.aborted) return;
    yield { type: 'modelContentBlockStopEvent' };
    yield { type: 'modelMessageStopEvent', stopReason: useTool ? 'toolUse' : 'endTurn' };
    yield { type: 'modelMetadataEvent', usage: { inputTokens: 3, outputTokens: 2, totalTokens: 5 } };
  }
}
export function agent(id, mode = 'text') {
  return new Agent({ id, name: id, model: new FixtureModel(mode), systemPrompt: SECRET, printer: false, traceAttributes: { customSecret: SECRET },
    tools: mode === 'tool' ? [new FunctionTool({ name: 'echo', description: 'Echo',
      inputSchema: { type: 'object', properties: { text: { type: 'string' } } },
      callback: async ({ text }) => { await Promise.resolve(); return text; },
    })] : [],
  });
}
