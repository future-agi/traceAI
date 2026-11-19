(async () => {
  let body;
  const speech = process.env.MODE === 'tts';
  if (speech) body = JSON.stringify({ model: 'tts-model', input: 'private transcript', voice: 'alloy' });
  else { body = new FormData(); body.set('model', 'whisper-1'); body.set('file', new Blob([new Uint8Array(8)], { type: 'audio/wav' }), 'audio.wav'); }
  const response = await fetch(process.env.MOCK_URL + (speech ? '/v1/audio/speech' : '/v1/audio/transcriptions'), { method: 'POST', body, ...(speech ? { headers: { 'content-type': 'application/json' } } : {}) });
  await response.arrayBuffer(); console.log('application ran');
})().catch(error => { console.error(error); process.exitCode = 1; });
