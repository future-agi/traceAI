// A long-running app with no tracing code and no SIGTERM handler of its own.
(async () => {
  const body = { model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] };
  await (await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })).text();
  console.log('ready');
  setInterval(() => {}, 1000);
})();
