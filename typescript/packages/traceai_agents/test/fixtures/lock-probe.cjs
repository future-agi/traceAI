// Test-only independent connection; exit 2 means the real OS lock refused it.
const Database = require("better-sqlite3");
let db;
try {
  db = new Database(process.argv[2], { timeout: 0 });
  db.exec("BEGIN EXCLUSIVE; ROLLBACK;");
  process.exitCode = 0;
} catch (error) {
  process.exitCode = error.code === "SQLITE_BUSY" ? 2 : 1;
} finally {
  db?.close();
}
