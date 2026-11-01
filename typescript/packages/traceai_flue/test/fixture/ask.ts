import { init } from '@flue/runtime';
import { start } from '@flue/runtime/node';
import { Weather } from './src/agents/weather.ts';
import { tracing } from './src/telemetry.ts';
const runtime = await start({ agents: [Weather] });
try {
  const agent = init(Weather, { id: 'standalone' });
  const receipt = await agent.dispatch('PRIVATE_QUESTION');
  const reply = await agent.read(receipt);
  console.log(reply.text);
} finally {
  await runtime[Symbol.asyncDispose]();
  if (process.env.EXPLICIT_SHUTDOWN === '1') await tracing.shutdown();
}
