// Deliberately contains no tracing imports or init calls.
(async () => {
  const body = { model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello private text' }], stream: process.env.MODE === 'stream' };
  const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const bytes = Buffer.from(await response.arrayBuffer());
  let headersImmutable = false;
  try { response.headers.set('x-test-guard', 'value'); } catch { headersImmutable = true; }
  console.log(JSON.stringify({ bytes: bytes.toString('base64'), url: response.url, status: response.status, bodyUsed: response.bodyUsed, headersImmutable }));
})().catch(e => { console.error(e); process.exitCode = 1; });
