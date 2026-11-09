// Real loopback Apollo requests; instrumentation must load before Apollo/GraphQL.
const assert = require('node:assert/strict');
const telemetry = require('@traceai/apollo-graphql').configure({ depth: 5 });
(async () => {
  const { ApolloServer } = await import('@apollo/server');
  const { startStandaloneServer } = await import('@apollo/server/standalone');
  const requests = [];
  const server = new ApolloServer({
    stopOnTerminationSignals: false,
    typeDefs: 'type Query { profile: Profile! } type Profile { name: String!, items: [Profile!]! }',
    resolvers: { Query: { profile: () => ({ name: 'synthetic', items: [{ name: 'nested', items: [] }] }) } },
    plugins: [{ async requestDidStart({ request }) { requests.push(request); } }],
  });
  const { url } = await startStandaloneServer(server, { listen: { host: '127.0.0.1', port: 0 } });
  try {
    const query = `query PRIVATE_OPERATION_ID_28471 {
      PRIVATE_ALIAS_ID_91823: profile {
        PRIVATE_NESTED_ALIAS_71234: name
        PRIVATE_LIST_ALIAS_65231: items { PRIVATE_ITEM_ALIAS_36129: name }
      }
    }`;
    // Repeated requests exercise Apollo's operation cache as well as execution.
    for (let i = 0; i < 2; i++) {
      const response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ query, operationName: 'PRIVATE_OPERATION_ID_28471' }) });
      assert.equal(response.status, 200);
      assert.deepEqual(await response.json(), { data: { PRIVATE_ALIAS_ID_91823: {
        PRIVATE_NESTED_ALIAS_71234: 'synthetic', PRIVATE_LIST_ALIAS_65231: [{ PRIVATE_ITEM_ALIAS_36129: 'nested' }],
      } } });
    }
    assert.equal(requests.length, 2);
    assert.ok(requests.every(request => request.query === query));
    assert.ok(requests.every(request => request.operationName === 'PRIVATE_OPERATION_ID_28471'));
  } finally {
    await server.stop();
    assert.equal(await telemetry.forceFlush(), true);
    assert.equal(await telemetry.shutdown(), true);
  }
  console.log(JSON.stringify({ requests }));
})().catch(error => { console.error(error); process.exitCode = 1; });
