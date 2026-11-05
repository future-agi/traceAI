import { execFileSync } from "child_process";
import { resolve } from "path";

// A fresh process exercises the distributed CJS entry point and require hooks,
// rather than Jest's module loader or manuallyInstrument(). Build before testing.
test("auto-load patches pg once and disable/enable restores it", () => {
  const output = execFileSync(process.execPath, ["-e", `
    const assert = require('node:assert/strict');
    const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
    const { PgVectorInstrumentation, isPatched } = require('./dist/src');
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new PgVectorInstrumentation({ traceConfig: { hideInputs: true } });
    instrumentation.setTracerProvider(provider);
    const pg = require('pg');
    assert.equal(isPatched(), true);
    const wrapper = pg.Client.prototype.query;
    instrumentation.disable();
    const original = pg.Client.prototype.query;
    assert.notEqual(wrapper, original);
    assert.equal(isPatched(), false);
    instrumentation.enable();
    const client = new pg.Client({ host: '127.0.0.1', user: 'synthetic' });
    let query;
    client._pulseQueryQueue = () => { query = (client._queryQueue ?? client.queryQueue).shift(); };
    const pending = client.query('SELECT id FROM docs ORDER BY embedding <-> $1', ['[1,2]']);
    query.callback(null, { rows: [], rowCount: 0 });
    pending.then(async () => {
      assert.equal(exporter.getFinishedSpans().length, 1);
      assert.equal(exporter.getFinishedSpans()[0].attributes['db.vector.query.dimensions'], 2);
      instrumentation.disable();
      assert.equal(pg.Client.prototype.query, original);
      await provider.shutdown();
      console.log('autoload passed');
    }).catch(error => { console.error(error); process.exitCode = 1; });
  `], { cwd: resolve(__dirname, "../.."), encoding: "utf8", timeout: 15000 });
  expect(output).toContain("autoload passed");
});
