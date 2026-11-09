import { expect } from 'vitest';
import { createHarness, describeEval } from 'vitest-evals';
const harness = createHarness({ name: 'fixture-harness', run: async ({ input }) => ({
  output: `reply:${input}`, events: [
    { type: 'message', role: 'user', content: 'private prompt' },
    { type: 'message', role: 'assistant', content: 'private answer' },
  ], usage: { provider: 'synthetic', model: 'model-1', inputTokens: 3, outputTokens: 2 },
}) });
describeEval('reporter fixture', { harness }, it => {
  it('captures actual harness metadata', async ({ run }) => {
    const result = await run('private input');
    expect(result.output).toBe('reply:private input');
  });
});
