import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

// Node's actual CJS/ESM loaders, not Jest's module loader. Requires pnpm build.
const entry = resolve(__dirname, "../../dist/src/index.js");

test.each(["mongodb-floor", "mongodb", "mongodb-current"])("automatic module hooks for %s", (driver) => {
  const directory = mkdtempSync(join(tmpdir(), "traceai-mongodb-"));
  try {
    mkdirSync(join(directory, "node_modules"));
    const driverDirectory = dirname(require.resolve(`${driver}/package.json`));
    symlinkSync(driverDirectory, join(directory, "node_modules/mongodb"), "dir");
    execFileSync(process.execPath, ["-e", `
      const assert = require('node:assert/strict');
      const {createRequire} = require('node:module');
      const {MongoDBInstrumentation, isPatched} = require(process.argv[1]);
      const instrumentation = new MongoDBInstrumentation();
      const load = createRequire(process.argv[2] + '/app.cjs');
      const mongo = load('mongodb');
      const connect = mongo.MongoClient.prototype.connect;
      assert.equal(isPatched(), true);
      assert.equal(connect.__wrapped, true);
      assert.equal(mongo.Collection.prototype.find.__wrapped, undefined);
      const pool = load('mongodb/lib/cmap/connection_pool.js').ConnectionPool.prototype;
      const major = Number(load('mongodb/package.json').version.split('.')[0]);
      if (major === 5) assert.equal(pool.checkOut.__wrapped, true);
      instrumentation.disable();
      assert.notEqual(mongo.MongoClient.prototype.connect, connect);
      assert.equal(pool.checkOut.__wrapped, undefined);
      assert.equal(isPatched(), false);
    `, entry, directory], { timeout: 10000, stdio: "pipe" });
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test.each(["mongodb-floor", "mongodb-current"])("native ESM preloaded module is supported for %s", (driver) => {
  execFileSync(process.execPath, ["--input-type=module", "-e", `
    import assert from 'node:assert/strict';
    import {pathToFileURL} from 'node:url';
    const mongo = await import(pathToFileURL(process.argv[2]).href);
    const original = mongo.MongoClient.prototype.connect;
    const {MongoDBInstrumentation} = await import(pathToFileURL(process.argv[1]).href);
    const instrumentation = new MongoDBInstrumentation();
    instrumentation.manuallyInstrument(mongo);
    assert.notEqual(mongo.MongoClient.prototype.connect, original);
    const client = new mongo.MongoClient('mongodb://127.0.0.1:1');
    const cursor = client.db('synthetic').collection('rows').find({}).limit(1);
    assert.equal(typeof cursor.toArray, 'function');
    await cursor.close();
    await client.close();
    instrumentation.disable();
    assert.equal(mongo.MongoClient.prototype.connect, original);
  `, entry, require.resolve(driver)], { timeout: 10000, stdio: "pipe" });
});
