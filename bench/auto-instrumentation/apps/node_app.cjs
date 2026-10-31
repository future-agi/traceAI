'use strict';
console.log(JSON.stringify({ kind: 'ready' }));
if (process.env.BENCH_STARTUP_ONLY === '1') process.exit(0);
const base = process.env.BENCH_PROVIDER;
const now = () => process.hrtime.bigint();
(async () => {
  const n = Number(process.env.BENCH_N), warmup = Number(process.env.BENCH_WARMUP);
  for (const stream of [false, true]) {
    for (let i = 0; i < n + warmup; i++) {
      const id = `${process.env.BENCH_PAIR}-${Number(stream)}-${i}`;
      const request = { model: 'gpt-4o-mini', messages: [{ role: 'user', content: id }], stream,
                        stream_options: { include_usage: true } };
      const chunks = []; let first;
      const start = now();
      const response = await fetch(base + '/v1/chat/completions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request)
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const reader = response.body.getReader();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value.length && first === undefined) first = now();
        chunks.push(Buffer.from(value));
      }
      const end = now();
      console.log(JSON.stringify({ kind: 'call', id, stream, warmup: i < warmup,
        latency_us: Number(end - start) / 1000, ttfc_us: Number(first - start) / 1000,
        body_b64: Buffer.concat(chunks).toString('base64') }));
    }
  }
  for (const options of [null, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: '{"not_ai":true}' }]) {
    const response = await fetch(base + (options ? '/v1/chat/completions' : '/health'), options || {});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    await response.arrayBuffer();
  }
  console.log('{"kind":"done"}');
  await new Promise(resolve => setTimeout(resolve, Number(process.env.BENCH_SETTLE_SECONDS) * 1000));
})().catch(error => { console.error(error); process.exitCode = 1; });
