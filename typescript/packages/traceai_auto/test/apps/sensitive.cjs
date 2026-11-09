(async () => {
  const body = { model: 'gpt-fixture', api_key: 'arbitrary-provider-token', messages: [{ role: 'user', content: [
    { type: 'text', text: process.env.FI_API_KEY },
    { type: 'input_audio', input_audio: { data: 'c2VjcmV0YXVkaW8=', format: 'wav' } }
  ] }] };
  const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json', authorization: 'Bearer private-header-token' }, body: JSON.stringify(body) });
  await response.text(); console.log('application ran');
})().catch(e => { console.error(e); process.exitCode = 1; });
