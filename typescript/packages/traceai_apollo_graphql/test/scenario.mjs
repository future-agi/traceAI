// Static ESM Apollo imports intentionally exercise Server 5 standalone's HTTP binding.
import { ApolloServer } from '@apollo/server';
import { startStandaloneServer } from '@apollo/server/standalone';
import { trace, metrics } from '@opentelemetry/api';
const tracer = trace.getTracer('application');
const server = new ApolloServer({
  stopOnTerminationSignals: false,
  typeDefs: `type Query { hello: String, profile(id: ID!): Profile, boom: String, partial: Profile }
    type Profile { name: String, bad: String }`,
  resolvers: {
    Query: {
      hello: () => 'world',
      profile: () => tracer.startActiveSpan('load-profile', async span => {
        try {
          span.setAttribute('application.owned', 'KEEP_APP_ATTRIBUTE');
          await new Promise(resolve => setTimeout(resolve, 160));
          return { name: 'Ada' };
        } finally { span.end(); }
      }),
      boom: () => { throw new Error('PRIVATE_ERROR'); },
      partial: () => ({ name: 'Ada' }),
    },
    Profile: { bad: () => { throw new Error('PRIVATE_NESTED_ERROR'); } },
  },
});
const { url } = await startStandaloneServer(server, { listen: { host: '127.0.0.1', port: 0 } });
if (process.env.SIGNAL_TEST) {
  process.once('SIGTERM', async () => {
    await server.stop();
    console.log('STOP ' + await globalThis.telemetry.shutdown());
  });
  console.log('READY ' + url);
} else {
  const post = async (query, variables, headers = {}) => (await fetch(url, {
    method: 'POST', headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify({ query, variables }),
  })).json();
  const replies = await Promise.all([
    post('query Hello { hello }'),
    post('query Profile($id: ID!) { profile(id: $id) { name } }', { id: 'PRIVATE_VARIABLE' }),
    post('query Profile($id: ID!) { profile(id: $id) { name } }', { id: 'SECOND_PRIVATE_VARIABLE' }),
  ]);
  replies.push(await post('query Hello { hello }')); // cached parse/validation
  replies.push(await post('query Literal { profile(id: "PRIVATE_LITERAL") { name } }'));
  replies.push(await post('query Boom { boom }'));
  replies.push(await post('query Partial { partial { name bad } }'));
  replies.push(await post('query Remote { hello }', undefined, {
    traceparent: '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01',
  }));
  replies.push(await post('query Invalid { doesNotExist }'));
  replies.push(await post('query Syntax {'));
  const forceFlushed = process.env.TEST_FORCE_FLUSH ? await globalThis.telemetry.forceFlush() : undefined;
  await server.stop();
  const start = Date.now();
  const flushed = await globalThis.telemetry.shutdown();
  console.log('RESULT ' + JSON.stringify({
    replies, flushed, forceFlushed, repeatedShutdown: await globalThis.telemetry.shutdown(),
    shutdownMs: Date.now() - start, meter: metrics.getMeterProvider().constructor.name,
  }));
}
