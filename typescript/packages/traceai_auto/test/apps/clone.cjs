// Real fetch metadata and immutable guards must survive repeated native clones.
(async () => {
  const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({model:'gpt-fixture',messages:[]}) });
  const clone = response.clone(), again = clone.clone();
  const metadata = [];
  for (const item of [response, clone, again]) {
    let immutable = false;
    try { item.headers.set('x-probe', 'value'); } catch { immutable = true; }
    metadata.push({url:item.url,type:item.type,redirected:item.redirected,immutable,body:await item.text()});
  }
  console.log(JSON.stringify(metadata));
})().catch(error => { console.error(error); process.exitCode = 1; });
