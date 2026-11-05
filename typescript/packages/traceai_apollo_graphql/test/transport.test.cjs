const assert = require('node:assert/strict');
if (process.argv[2] === 'child') {
  const telemetry = require('@traceai/apollo-graphql').configure({
    apiKey: 'synthetic', secretKey: 'synthetic', baseUrl: process.argv[3],
  });
  const tracer = require('@opentelemetry/api').trace.getTracer('application');
  const mode = process.argv[4];
  for (let i = 0; i < (mode === 'concurrent' ? 600 : 1); i++) tracer.startSpan('transport-probe').end();
  (async () => {
    const started = Date.now();
    const flushed = mode === 'trickle-flush-first' || mode === 'recover' ? await telemetry.forceFlush() : undefined;
    // An expired batch must not disable later exports or their fresh agents.
    if (mode === 'recover') {
      assert.equal(flushed, false);
      tracer.startSpan('after-timeout').end();
      // forceFlush also reports the earlier batch failure once, then resets it.
      await telemetry.forceFlush();
      assert.equal(await telemetry.forceFlush(), true);
    }
    const first = telemetry.shutdown();
    assert.equal(first, telemetry.shutdown(), 'shutdown returns the same promise');
    const ok = await first;
    assert.equal(ok, await telemetry.shutdown());
    console.log(JSON.stringify({ ok, flushed, elapsed: Date.now() - started }));
  })().catch(error => { console.error(error); process.exitCode = 1; });
} else {
  const { test } = require('node:test');
  const http = require('node:http');
  const { spawn } = require('node:child_process');
  for (const mode of ['normal', 'trickle', 'trickle-flush-first', 'no-headers', 'retry-trickle', 'concurrent', 'recover']) {
    test(`natural process exit after ${mode} collector response`, { timeout: 15000 }, async () => {
      const sockets = new Set();
      let requests = 0;
      const server = http.createServer((req, res) => {
        const requestNumber = ++requests;
        req.resume();
        req.on('end', () => {
          if (mode === 'no-headers') return;
          if (mode === 'retry-trickle' && requestNumber === 1) { res.writeHead(503); res.end('{}'); return; }
          res.writeHead(200, { 'content-type': 'application/json' });
          if (mode === 'normal' || (['concurrent', 'recover'].includes(mode) && requestNumber > 1)) {
            res.end('{}'); return;
          }
          res.write('{');
          const timer = setInterval(() => res.write(' '), 100);
          res.on('close', () => clearInterval(timer));
        });
      });
      server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
      const child = spawn(process.execPath, [__filename, 'child', `http://127.0.0.1:${server.address().port}`,
        mode], { cwd: require('node:path').resolve(__dirname, '..') });
      let stdout = '', stderr = '', killed = false;
      child.stdout.on('data', data => { stdout += data; });
      child.stderr.on('data', data => { stderr += data; });
      const timer = setTimeout(() => { killed = true; child.kill('SIGKILL'); }, 10000);
      try {
        const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
        assert.equal(killed, false, `shutdown must release its transport, not just resolve: ${stdout} ${stderr}`);
        assert.equal(code, 0, stderr);
        assert.ok(requests > 0, 'collector really received the export');
        if (['retry-trickle', 'concurrent', 'recover'].includes(mode)) assert.equal(requests, 2);
        const result = JSON.parse(stdout.trim());
        // A 200 with an unfinished body is not a completed export.
        assert.equal(mode.endsWith('flush-first') ? result.flushed : result.ok, ['normal', 'recover'].includes(mode));
        assert.ok(result.elapsed < 7000, JSON.stringify(result));
      } finally {
        clearTimeout(timer);
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
        for (const socket of sockets) socket.destroy();
        await new Promise(resolve => server.close(resolve));
      }
    });
  }
}
