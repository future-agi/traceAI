// Synthetic process barrier: prove ownership precedes database header inspection.
// This fixture is excluded from the package. No production timing is modified.
const fs = require("node:fs");
const Database = require("better-sqlite3");
const { Spool } = require("../../dist/src/storage/store.js");
const [root, release] = process.argv.slice(2);
const pragma = Database.prototype.pragma;
const pause = new Int32Array(new SharedArrayBuffer(4));
Database.prototype.pragma = function (sql, options) {
  const value = pragma.call(this, sql, options);
  if (sql === "page_size") {
    process.send({ phase: "inspection" });
    const until = Date.now() + 5000;
    while (!fs.existsSync(release)) {
      if (Date.now() >= until) throw new Error("FIXTURE_BARRIER_TIMEOUT");
      Atomics.wait(pause, 0, 0, 10);
    }
  }
  return value;
};
let spool;
try {
  spool = new Spool(root, "0.1.0", true);
  process.send({ phase: "ready" });
} catch (error) {
  process.send({ phase: "held", code: error.code });
}
process.on("disconnect", () => {
  spool?.close();
  process.exit(0);
});
